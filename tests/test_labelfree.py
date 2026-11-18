"""Validation without labels is only worth having if it can catch a bad correlator.

Each check below is written as a case where the answer is known by construction, so the
scorer's claim to be measuring something is itself tested:

  * a correlator that links only within declarations must score 1.0, and one that links
    across them must be charged for it;
  * a link that recurs across windows must be separated from one that appears once;
  * a sample of one from a population of one must have a wide interval, not a point;
  * a link supported by one artefact class must be dropped by two-class corroboration.

The last one matters most: a single copied favicon is an attack, not evidence.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from eval.labelfree import (
    adjudicated_precision,
    audit_sample,
    canonical,
    declared_negatives,
    false_link_rate,
    require_kinds,
    score_against_declared,
    temporal_stability,
    wilson_interval,
)

# --------------------------------------------------------------------------- #
# canonical form
# --------------------------------------------------------------------------- #


def test_canonical_is_direction_free():
    assert canonical("b", "a") == canonical("a", "b") == ("a", "b")


# --------------------------------------------------------------------------- #
# self-declared ground truth
# --------------------------------------------------------------------------- #


DECLARED = {
    "mirror-list": {"alpha-1", "alpha-2", "alpha-3"},
    "about-page": {"beta-1", "beta-2"},
}


def test_links_inside_declarations_are_true():
    score = score_against_declared(
        [("alpha-1", "alpha-2"), ("alpha-2", "alpha-3"), ("beta-1", "beta-2")],
        DECLARED,
    )
    assert score["tp"] == 3
    assert score["fp"] == 0
    assert score["precision"] == 1.0
    # alpha has three members, so it declares three pairs; the input names two of them
    assert score["recall"] == 0.75
    assert score["missed_declared_pairs"] == 1


def test_links_across_declarations_are_charged():
    score = score_against_declared([("alpha-1", "beta-1")], DECLARED)
    assert score["tp"] == 0
    assert score["fp"] == 1
    assert score["precision"] == 0.0


def test_undeclared_endpoints_are_unknown_not_wrong():
    """A link to a service nobody declared cannot be scored, and must not be an FP."""
    score = score_against_declared([("alpha-1", "mystery-1")], DECLARED)
    assert score["unlabelled"] == 1
    assert score["fp"] == 0
    assert score["scored"] == 0
    assert score["precision"] is None


def test_recall_counts_declared_pairs_that_were_missed():
    score = score_against_declared([("alpha-1", "alpha-2")], DECLARED)
    assert score["declared_pairs"] == 4  # 3 choose 2 inside alpha, 1 inside beta
    assert score["recall"] == 0.25
    assert score["missed_declared_pairs"] == 3


def test_duplicate_and_reversed_links_are_one_link():
    score = score_against_declared(
        [("alpha-1", "alpha-2"), ("alpha-2", "alpha-1")], DECLARED
    )
    assert score["proposed"] == 1
    assert score["tp"] == 1


def test_declared_negatives_and_false_link_rate():
    services = ["alpha-1", "alpha-2", "beta-1", "gamma-9"]
    negatives = declared_negatives(services, DECLARED)
    # alpha-1/alpha-2 are same declaration; beta-1 and alpha-* are cross; gamma is unknown
    assert ("alpha-1", "beta-1") in negatives
    assert ("alpha-1", "alpha-2") not in negatives
    assert all("gamma-9" not in pair for pair in negatives)

    rate = false_link_rate([("alpha-1", "beta-1")], negatives)
    assert rate["linked"] == 1
    assert rate["rate"] == 1 / len(negatives)


# --------------------------------------------------------------------------- #
# temporal stability
# --------------------------------------------------------------------------- #


def test_one_off_links_do_not_survive_recurrence():
    windows = [
        ("w1", [("a", "b"), ("c", "d")]),
        ("w2", [("a", "b"), ("e", "f")]),
    ]
    result = temporal_stability(windows)
    assert result["links_seen"] == 3  # a-b, c-d, e-f
    assert result["stable_links"] == 1
    assert result["stable_fraction"] == pytest.approx(1 / 3)
    assert result["recurrence"][("a", "b")] == 2


def test_jaccard_is_between_zero_and_one_and_identical_crawls_overlap_fully():
    same = temporal_stability([("w1", [("a", "b")]), ("w2", [("a", "b")])])
    assert same["mean_jaccard"] == 1.0

    disjoint = temporal_stability([("w1", [("a", "b")]), ("w2", [("c", "d")])])
    assert disjoint["mean_jaccard"] == 0.0


def test_single_window_has_no_drift_measure():
    result = temporal_stability([("w1", [("a", "b")])])
    assert result["mean_jaccard"] is None
    assert result["stable_links"] == 0


# --------------------------------------------------------------------------- #
# audit sampling
# --------------------------------------------------------------------------- #


def test_wilson_interval_is_bounded_and_widens_at_small_n():
    low, high = wilson_interval(10, 10)
    assert 0.0 <= low <= high <= 1.0
    assert high == pytest.approx(1.0)
    assert low < 0.75, "ten out of ten is not a point estimate at 100%"

    tight = wilson_interval(950, 1000)
    assert tight[1] - tight[0] < 0.05


def test_wilson_refuses_impossible_counts():
    with pytest.raises(ValueError):
        wilson_interval(1, 0)
    with pytest.raises(ValueError):
        wilson_interval(5, 4)


def test_audit_sample_is_reproducible_and_sorted():
    population = [(f"s{i:02d}", f"t{i:02d}") for i in range(10)]
    first = audit_sample(population, 4, seed=7)
    second = audit_sample(population, 4, seed=7)
    assert [row["link"] for row in first] == [row["link"] for row in second]
    assert [tuple(row["link"]) for row in first] == sorted(
        tuple(row["link"]) for row in first
    )
    assert all(row["verdict"] is None for row in first)


def test_audit_sample_refuses_impossible_requests():
    with pytest.raises(ValueError):
        audit_sample([("a", "b")], 0)
    with pytest.raises(ValueError):
        audit_sample([("a", "b")], 5)


def test_adjudicated_precision_reports_its_interval():
    result = adjudicated_precision([True, True, False, True, True])
    assert result["precision"] == 0.8
    assert result["adjudicated"] == 5
    assert result["ci95"][0] < 0.8 < result["ci95"][1]


def test_adjudicated_precision_refuses_empty():
    with pytest.raises(ValueError):
        adjudicated_precision([])


# --------------------------------------------------------------------------- #
# two-class corroboration
# --------------------------------------------------------------------------- #


def test_two_class_corroboration_drops_a_single_copied_artefact():
    records = [
        {"a": "x1", "b": "x2", "kinds": ["favicon", "analytics"]},
        {"a": "y1", "b": "y2", "kinds": ["favicon"]},
    ]
    result = require_kinds(records, min_kinds=2)
    assert [item["link"] for item in result["kept"]] == [["x1", "x2"]]
    assert [item["link"] for item in result["dropped"]] == [["y1", "y2"]]


def test_corroboration_counts_classes_not_values():
    """Ten values of one class are still one class."""
    records = [{"a": "a", "b": "b", "kinds": ["favicon", "favicon", "favicon"]}]
    result = require_kinds(records, min_kinds=2)
    assert result["kept"] == []


def test_corroboration_rejects_a_zero_threshold():
    with pytest.raises(ValueError):
        require_kinds([], min_kinds=0)


# --------------------------------------------------------------------------- #
# class strength
# --------------------------------------------------------------------------- #

from eval.labelfree import admit_link, apply_policy


def test_one_hard_class_is_enough():
    """A shared SSH host key cannot be copied without holding the key."""
    admitted, reason = admit_link({"ssh_hostkey_sha256"})
    assert admitted is True
    assert "hard" in reason


def test_one_soft_class_is_not_enough():
    """The single copied favicon is the attack this rule exists to refuse."""
    admitted, reason = admit_link({"favicon_sha256"})
    assert admitted is False
    assert "single soft" in reason


def test_two_soft_classes_corroborate():
    admitted, reason = admit_link({"favicon_sha256", "analytics_id"})
    assert admitted is True
    assert "2 independent soft" in reason


def test_duplicate_soft_values_are_one_class():
    admitted, _ = admit_link({"favicon_sha256"})
    assert admitted is False


def test_unknown_class_is_treated_as_soft_and_needs_corroboration():
    alone, _ = admit_link({"some_new_signal"})
    assert alone is False
    together, _ = admit_link({"some_new_signal", "analytics_id"})
    assert together is True


def test_hard_beats_soft_in_the_reason():
    admitted, reason = admit_link({"tls_spki_sha256", "favicon_sha256"})
    assert admitted is True
    assert "tls_spki_sha256" in reason


def test_apply_policy_records_a_reason_on_both_sides():
    result = apply_policy(
        [
            {"a": "a", "b": "b", "kinds": ["ssh_hostkey_sha256"]},
            {"a": "c", "b": "d", "kinds": ["favicon_sha256"]},
        ]
    )
    assert result["total"] == 2
    assert len(result["kept"]) == 1 and len(result["dropped"]) == 1
    assert result["kept"][0]["reason"]
    assert result["dropped"][0]["reason"]


def test_policy_keeps_every_true_link_on_the_farm():
    """Pins the measured claim: strength weighting costs no recall here.

    Requiring two classes of any kind drops half of these links, because a shared SSH
    host key or application bundle hash is one sufficient class. This asserts the rule
    that replaced it keeps all twelve, so a later edit cannot quietly lose recall.
    """
    from eval.detectors.correlation import artifact_frequencies, rank_services
    from eval.labelfree import canonical
    from farm.generator import generate_farm
    from farm.model import FarmConfig

    farm = generate_farm(FarmConfig())
    frequencies = artifact_frequencies(farm)
    seen = {}
    for service in farm.services:
        if not service.cluster_id:
            continue
        for candidate, _ in rank_services(
            farm,
            service.service_id,
            farm.correlation_pool(service.service_id),
            max_artifact_frequency=3,
        ):
            left = farm.service(service.service_id).shared_artifacts
            right = farm.service(candidate).shared_artifacts
            kinds = {
                key
                for key in left.keys() & right.keys()
                if left[key] == right[key] and frequencies[left[key]] <= 3
            }
            seen[canonical(service.service_id, candidate)] = sorted(kinds)

    records = [{"a": a, "b": b, "kinds": kinds} for (a, b), kinds in seen.items()]
    result = apply_policy(records)
    assert result["total"] == 12
    assert len(result["kept"]) == 12
    assert result["dropped"] == []
