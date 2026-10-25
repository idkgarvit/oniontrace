"""Metric primitives for the ONIONTRACE evaluation harness.

Every function here exists to answer a question Part E asks, and the docstrings
record *which* question, so a metric cannot quietly drift away from the claim it
supports.

Two conventions, applied everywhere:

  * a **ranking** is a list of candidate ids ordered best-first;
  * a **relevant** set is the ground truth for that query.

Proportions are reported with a **Wilson score interval** rather than a bare point
estimate. The default farm is small (12 clustered services, 20 personas), so the
interval is wide and saying so is more honest than quoting a bare percentage that
implies precision the sample cannot support.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

# z for a two-sided 95% interval.
Z_95 = 1.959963984540054


@dataclass(frozen=True)
class Proportion:
    """A proportion with a Wilson score interval.

    Reported instead of a bare float so that "precision@1 = 0.75" always arrives
    with the sample size and uncertainty attached.
    """

    successes: int
    total: int
    value: float
    lower: float
    upper: float
    #: Queries the system declined to answer, counted in `total` as misses rather than
    #: dropped from it. Zero for metrics where abstention has no meaning.
    abstained: int = 0
    #: Queries with no right answer to find, excluded from `total` because restraint is
    #: the correct behaviour there; scored by `score_abstention` instead.
    skipped_no_answer: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "successes": self.successes,
            "total": self.total,
            "value": round(self.value, 6),
            "ci95": [round(self.lower, 6), round(self.upper, 6)],
            "abstained": self.abstained,
            "skipped_no_answer": self.skipped_no_answer,
        }


def wilson_interval(successes: int, total: int, z: float = Z_95) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion.

    Chosen over the normal approximation because it stays inside [0, 1] and behaves
    sensibly at small n and at proportions near 0 or 1 -- both of which occur here,
    and 0 is a target value for the false-merge rate.
    """
    if total <= 0:
        return (0.0, 0.0)
    phat = successes / total
    denom = 1.0 + z * z / total
    center = (phat + z * z / (2 * total)) / denom
    margin = (
        z * math.sqrt(phat * (1 - phat) / total + z * z / (4 * total * total)) / denom
    )
    return (max(0.0, center - margin), min(1.0, center + margin))


def proportion(
    successes: int,
    total: int,
    *,
    abstained: int = 0,
    skipped_no_answer: int = 0,
) -> Proportion:
    if total <= 0:
        return Proportion(0, 0, 0.0, 0.0, 0.0, abstained, skipped_no_answer)
    lower, upper = wilson_interval(successes, total)
    return Proportion(
        successes, total, successes / total, lower, upper, abstained, skipped_no_answer
    )


# --------------------------------------------------------------------------- #
# Ranking metrics
# --------------------------------------------------------------------------- #


def precision_at_1(
    rankings: Sequence[Sequence[str]], relevant: Sequence[set[str]]
) -> Proportion:
    """Of the top-ranked candidate for each query, how many were relevant.

    The headline ranking metric. Part E uses top-1 rather than top-10 precisely
    because a top-10 list over few candidates is satisfied by construction.

    **Abstention is scored by whether an answer existed.** Part E1's own table says
    abstention is the correct behaviour only for "services with no cluster-mate
    present". So:

      * `truth` is non-empty -- a right answer existed and the system returned
        nothing -- that is a **miss**, and it stays in the denominator. The first
        version dropped these with `if not ranked: continue`, so a system that
        abstained on every hard query and answered one easy one reported 1.00 over
        n=1. That is the opposite of what this project claims about restraint, and
        it made n depend on the system's own behaviour, so two variants were not
        comparable over the same query set.
      * `truth` is empty -- no right answer exists -- abstaining is correct, and the
        query belongs to `score_abstention` rather than here. It is skipped, with
        the count reported, so the two are never conflated.

    `abstained` is always reported, so a reader can see how many queries were
    declined rather than answered.
    """
    hits = 0
    total = 0
    abstained = 0
    skipped_no_answer = 0
    for ranked, truth in zip(rankings, relevant, strict=True):
        if not truth:
            # No right answer to find: restraint, not failure. Scored elsewhere.
            skipped_no_answer += 1
            continue
        total += 1
        if not ranked:
            abstained += 1
            continue
        if ranked[0] in truth:
            hits += 1
    return proportion(hits, total, abstained=abstained, skipped_no_answer=skipped_no_answer)


def hit_at_k(
    rankings: Sequence[Sequence[str]], relevant: Sequence[set[str]], k: int
) -> Proportion:
    """Of the queries, how many had a relevant candidate anywhere in the top k.

    One relevant candidate per query is assumed for the interval; use
    `recall_at_k` when a query has several.
    """
    hits = 0
    total = 0
    abstained = 0
    skipped_no_answer = 0
    for ranked, truth in zip(rankings, relevant, strict=True):
        if not truth:
            # No right answer to find: restraint, not failure. Scored elsewhere.
            skipped_no_answer += 1
            continue
        total += 1
        if not ranked:
            abstained += 1
            continue
        if truth & set(ranked[:k]):
            hits += 1
    return proportion(hits, total, abstained=abstained, skipped_no_answer=skipped_no_answer)


def recall_at_k(
    rankings: Sequence[Sequence[str]], relevant: Sequence[set[str]], k: int
) -> float:
    """Mean fraction of relevant candidates recovered in the top k.

    Multi-positive aware, so it is the correct partner for `hit_at_k` whenever a
    query can have more than one right answer -- correlation queries always do.
    """
    ratios: list[float] = []
    for ranked, truth in zip(rankings, relevant, strict=True):
        if not truth:
            continue
        ratios.append(len(truth & set(ranked[:k])) / len(truth))
    return sum(ratios) / len(ratios) if ratios else 0.0


def mean_reciprocal_rank(
    rankings: Sequence[Sequence[str]], relevant: Sequence[set[str]]
) -> float:
    """Mean of 1/rank of the first relevant candidate.

    Reported alongside precision@1 because a single number cannot distinguish a
    correct answer at rank 1 from one at rank 5, and that distinction is the whole
    difference between a lead and a haystack.
    """
    total = 0.0
    count = 0
    for ranked, truth in zip(rankings, relevant, strict=True):
        if not truth:
            continue
        count += 1
        for position, candidate in enumerate(ranked, start=1):
            if candidate in truth:
                total += 1.0 / position
                break
    return total / count if count else 0.0


# --------------------------------------------------------------------------- #
# Link scoring -- candidate links, with denominators written down
# --------------------------------------------------------------------------- #


@dataclass
class LinkScoring:
    """Outcome counts for a set of proposed links against a known truth set.

    Part E2 requires the denominator to be stated, because `false_merge_rate` is not
    a self-evident quantity:

        precision        = TP / (TP + FP)
        recall           = TP / (TP + FN)
        false_merge_rate = FP / (TP + FP)     <- identical to 1 - precision

    The last line is why the plan states one target, not two. "precision > 90% and
    false-merge < 3%" is a contradiction unless precision is > 97%, and writing both
    numbers down invites an evaluator to notice that.
    """

    threshold: float
    tp: int
    fp: int
    fn: int
    tn: int

    @property
    def proposed(self) -> int:
        return self.tp + self.fp

    @property
    def actual(self) -> int:
        return self.tp + self.fn

    @property
    def precision(self) -> float:
        return self.tp / self.proposed if self.proposed else 0.0

    @property
    def recall(self) -> float:
        return self.tp / self.actual if self.actual else 0.0

    @property
    def false_merge_rate(self) -> float:
        """FP / (TP + FP). The safety-critical quantity: an innocuous name in a report."""
        return self.fp / self.proposed if self.proposed else 0.0

    @property
    def false_omit_rate(self) -> float:
        """FN / (TP + FN). A miss. Regrettable, but not a wrongful accusation."""
        return self.fn / self.actual if self.actual else 0.0

    @property
    def precision_ci(self) -> tuple[float, float]:
        if self.proposed == 0:
            return (0.0, 0.0)
        return wilson_interval(self.tp, self.proposed)

    def to_dict(self) -> dict[str, Any]:
        lower, upper = self.precision_ci
        return {
            "threshold": round(self.threshold, 6),
            "tp": self.tp,
            "fp": self.fp,
            "fn": self.fn,
            "tn": self.tn,
            "proposed": self.proposed,
            "actual": self.actual,
            "precision": round(self.precision, 6),
            "precision_ci95": [round(lower, 6), round(upper, 6)],
            "recall": round(self.recall, 6),
            "false_merge_rate": round(self.false_merge_rate, 6),
            "false_omit_rate": round(self.false_omit_rate, 6),
        }


def score_links(
    proposed: Iterable[tuple[str, str]],
    truth: Iterable[tuple[str, str]],
    universe: Iterable[tuple[str, str]] | None = None,
    threshold: float = 0.0,
) -> LinkScoring:
    """Score proposed links against truth, over a stated universe of pairs.

    `universe` is every pair the system was allowed to propose. When it is omitted it
    is taken to be `proposed | truth`, which makes TN zero -- acceptable for
    precision and recall, but pass the real universe when the true-negative count
    matters, because it changes what the numbers mean.
    """
    proposed_set = {tuple(sorted(p)) for p in proposed}
    truth_set = {tuple(sorted(p)) for p in truth}

    if universe is None:
        universe_set = proposed_set | truth_set
    else:
        universe_set = {tuple(sorted(p)) for p in universe}

    tp = len(proposed_set & truth_set)
    fp = len(proposed_set - truth_set)
    fn = len(truth_set - proposed_set)
    tn = len(universe_set - proposed_set - truth_set)
    return LinkScoring(threshold=threshold, tp=tp, fp=fp, fn=fn, tn=tn)


# --------------------------------------------------------------------------- #
# Abstention
# --------------------------------------------------------------------------- #


@dataclass
class AbstentionScoring:
    """How well the system declines to answer questions it cannot answer.

    Part D4 makes abstention cases first-class, and this is why: a system scored only
    on answerable queries has no way to be measured on restraint. For a tool that
    proposes candidate identities, declining is the safe behaviour.
    """

    total: int
    correct_abstentions: int
    false_assertions: int

    @property
    def abstention_rate(self) -> float:
        return self.correct_abstentions / self.total if self.total else 0.0

    @property
    def false_assertion_rate(self) -> float:
        return self.false_assertions / self.total if self.total else 0.0

    @property
    def abstention_ci(self) -> tuple[float, float]:
        if self.total == 0:
            return (0.0, 0.0)
        return wilson_interval(self.correct_abstentions, self.total)

    def to_dict(self) -> dict[str, Any]:
        lower, upper = self.abstention_ci
        return {
            "total": self.total,
            "correct_abstentions": self.correct_abstentions,
            "false_assertions": self.false_assertions,
            "abstention_rate": round(self.abstention_rate, 6),
            "abstention_ci95": [round(lower, 6), round(upper, 6)],
            "false_assertion_rate": round(self.false_assertion_rate, 6),
        }


def score_abstention(asserted: Sequence[bool]) -> AbstentionScoring:
    """Score abstention cases. `asserted[i]` is True if the system returned an answer.

    An assertion on an unanswerable query is a **false assertion**, which is the
    failure mode that produces a wrong name.
    """
    total = len(asserted)
    false_assertions = sum(1 for x in asserted if x)
    return AbstentionScoring(
        total=total,
        correct_abstentions=total - false_assertions,
        false_assertions=false_assertions,
    )