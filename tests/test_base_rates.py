"""The farm's one claim about the world, measured rather than chosen.

The farm plants stock defaults on every eligible service. That is a prevalence assumption, and
until now it was the only number in the correlation experiment with no source. These tests hold
the measurement to its source file, and hold the experiment to its conclusion when the
prevalence is dropped to the rate real certificates show.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval.harness import correlation_experiment
from farm.base_rates import (
    DESIGN_SHARE,
    MEASURED_POPULATION,
    MEASURED_SHARED_ARTEFACT_SHARE,
    MEASURED_TOP_ARTEFACT_SHARE,
    describe,
)
from farm.generator import generate_farm, widespread_targets
from farm.model import FarmConfig, Service

ROOT = Path(__file__).resolve().parents[1]
MEASUREMENT = json.loads(
    (ROOT / "data" / "evidence" / "real_world" / "base_rates.json").read_text(encoding="utf-8")
)


def _services(clusters: dict[str, int]) -> dict[str, list[Service]]:
    out: dict[str, list[Service]] = {}
    for cluster_id, count in clusters.items():
        out[cluster_id] = [
            Service(
                service_id=f"{cluster_id}-S{i}",
                cluster_id=cluster_id,
                address=f"{cluster_id}{i}.onion",
                title=f"{cluster_id} market {i}",
                topic="market",
            )
            for i in range(count)
        ]
    return out


# --- the numbers came from somewhere ------------------------------------------------


def test_the_measured_share_still_matches_the_measurement():
    """If the constant and the file disagree, one of them is stale and nobody would notice."""

    assert MEASUREMENT["certificates_measured"] == MEASURED_POPULATION
    assert MEASUREMENT["top_issuer_share"] == pytest.approx(MEASURED_TOP_ARTEFACT_SHARE)
    shared = MEASUREMENT["certificates_under_a_shared_issuer"] / MEASUREMENT["certificates_measured"]
    assert shared == pytest.approx(MEASURED_SHARED_ARTEFACT_SHARE, abs=1e-4)


def test_the_measurement_is_of_real_certificates():
    assert MEASUREMENT["artefact_class"] == "certificate issuer"
    assert MEASUREMENT["distinct_issuers"] > 1, "one issuer would make the share meaningless"
    assert MEASUREMENT["certificates_measured"] > 1000


def test_the_describe_line_names_both_rates():
    line = describe()
    assert "100.0%" in line and "59.2%" in line


# --- how prevalence turns into services ---------------------------------------------


def test_the_designed_share_is_everyone_in_cluster_order():
    eligible = _services({"A": 2, "B": 2})
    targets = widespread_targets(eligible, DESIGN_SHARE)
    assert [s.service_id for s in targets] == ["A-S0", "A-S1", "B-S0", "B-S1"]


def test_a_lower_share_is_dealt_across_clusters_not_taken_as_a_prefix():
    eligible = _services({"A": 3, "B": 3, "C": 3})
    targets = widespread_targets(eligible, 1 / 3)
    clusters = {service.cluster_id for service in targets}
    assert len(targets) == 3
    assert clusters == {"A", "B", "C"}, "a prefix would put the whole default in cluster A"


def test_a_share_too_small_to_share_anything_still_needs_two_services():
    eligible = _services({"A": 3, "B": 3})
    assert len(widespread_targets(eligible, 0.01)) == 2


def test_the_share_actually_changes_the_farm():
    full = generate_farm(FarmConfig(seed=20260925, widespread_default_share=DESIGN_SHARE))
    measured = generate_farm(
        FarmConfig(seed=20260925, widespread_default_share=MEASURED_TOP_ARTEFACT_SHARE)
    )
    full_count = sum(1 for negative in full.hard_negatives if "[widespread" in negative.why)
    measured_count = sum(1 for negative in measured.hard_negatives if "[widespread" in negative.why)
    assert measured_count < full_count
    assert measured_count == pytest.approx(round(MEASURED_TOP_ARTEFACT_SHARE * full_count), abs=1)


def test_the_same_seed_and_share_still_give_the_same_farm():
    first = generate_farm(FarmConfig(seed=7, widespread_default_share=0.5))
    second = generate_farm(FarmConfig(seed=7, widespread_default_share=0.5))
    assert [s.service_id for s in first.services] == [s.service_id for s in second.services]
    assert [n.fixture_id for n in first.hard_negatives] == [
        n.fixture_id for n in second.hard_negatives
    ]


# --- and the conclusion survives the drop -------------------------------------------


def test_the_cap_still_wins_at_the_measured_prevalence():
    """The claim that matters: the frequency cap is not just beating a 100% strawman."""

    farm = generate_farm(
        FarmConfig(seed=20260925, widespread_default_share=MEASURED_TOP_ARTEFACT_SHARE)
    )
    naive = correlation_experiment(farm, max_artifact_frequency=None)["link_scoring"]
    capped = correlation_experiment(farm, max_artifact_frequency=3)["link_scoring"]
    assert capped["precision"] == 1.0
    assert capped["false_merge_rate"] == 0.0
    assert naive["precision"] < capped["precision"]


def test_the_cap_wins_even_where_defaults_are_rare():
    farm = generate_farm(FarmConfig(seed=20260925, widespread_default_share=0.25))
    capped = correlation_experiment(farm, max_artifact_frequency=3)["link_scoring"]
    assert capped["precision"] == 1.0


def test_the_default_farm_is_the_designed_farm():
    """Nothing above changed the published condition by accident."""

    farm = generate_farm(FarmConfig(seed=20260925))
    assert FarmConfig().widespread_default_share == DESIGN_SHARE
    assert sum(1 for negative in farm.hard_negatives if "[widespread" in negative.why) == 18
