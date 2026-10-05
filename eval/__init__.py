"""ONIONTRACE evaluation harness.

Reads the synthetic farm's ground truth, scores the pipeline modules against it, and
emits results that carry their own reproduction command.

The harness is deliberately built **before** the modules it measures (Part G item 4).
Producing numbers after the fact means they cannot be defended; producing them from a
frozen harness means every one can be regenerated from a seed.

Structure:

  * `metrics`   -- ranking, link-scoring and abstention primitives, with intervals
  * `splits`    -- by-cluster held-out splits and frozen-threshold selection
  * `detectors` -- reference implementations of the modules under test
  * `harness`   -- orchestrates the run and writes the results file
"""

from eval.metrics import (
    AbstentionScoring,
    LinkScoring,
    Proportion,
    hit_at_k,
    mean_reciprocal_rank,
    precision_at_1,
    proportion,
    recall_at_k,
    score_abstention,
    score_links,
    wilson_interval,
)
from eval.splits import ClusterSplit, ThresholdDecision, choose_threshold, cluster_split

__all__ = [
    "AbstentionScoring",
    "ClusterSplit",
    "LinkScoring",
    "Proportion",
    "ThresholdDecision",
    "choose_threshold",
    "cluster_split",
    "hit_at_k",
    "mean_reciprocal_rank",
    "precision_at_1",
    "proportion",
    "recall_at_k",
    "score_abstention",
    "score_links",
    "wilson_interval",
]

__version__ = "0.1.0"
