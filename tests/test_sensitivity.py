"""The frequency-cap sweep has to be an honest sensitivity analysis, not a chart.

Two things make it honest, and both are pinned here:

  * the cap that the sweep reports as derived is the one the production rule computes,
    not a number the sweep picked because it looked best;
  * the sweep covers the whole range from "filter everything" to "filter nothing", so
    the reader sees the cost of being wrong in both directions rather than a single
    flattering point.

The regime compression is also tested, because a table that silently merges two
different results into one row would misreport the curve.

Everything runs offline against the seeded farm.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from eval.detectors.correlation import common_value_threshold
from eval.sensitivity import (
    largest_cluster,
    regimes,
    render_markdown,
    sweep,
    sweep_caps,
)
from farm.generator import generate_farm
from farm.model import FarmConfig


@pytest.fixture(scope="module")
def farm():
    return generate_farm(FarmConfig())


@pytest.fixture(scope="module")
def result(farm):
    return sweep(farm)


def test_sweep_starts_at_one_and_ends_where_filtering_stops(farm, result):
    caps = [row["cap"] for row in result["rows"]]
    assert caps[0] == 1
    assert caps == sorted(caps)
    assert caps == sweep_caps(farm)
    # the top cap leaves nothing filtered, so it must equal the naive row
    assert result["rows"][-1]["proposed"] == result["naive"]["proposed"]
    assert result["rows"][-1]["fp"] == result["naive"]["fp"]


def test_derived_cap_is_the_production_rule(farm, result):
    assert result["derived_cap"] == common_value_threshold(largest_cluster(farm))
    assert result["derived_cap"] == max(2, largest_cluster(farm))


def test_below_cluster_size_recall_collapses(farm, result):
    """Filtering below the cluster size must destroy recall, not quietly pass."""
    for row in result["rows"]:
        if row["cap"] < largest_cluster(farm):
            assert row["tp"] == 0
            assert row["recall"] == 0.0


def test_derived_cap_is_precise_on_the_farm(result):
    derived = result["derived_cap"]
    row = next(r for r in result["rows"] if r["cap"] == derived)
    assert row["precision"] == 1.0
    assert row["recall"] == 1.0
    assert row["fp"] == 0


def test_naive_row_is_the_unfiltered_correlator(farm, result):
    naive = result["naive"]
    assert naive["cap"] is None
    assert naive["recall"] == 1.0
    assert naive["fp"] > 0, "the farm must contain collisions for this to mean anything"


def test_regimes_cover_every_cap_exactly_once(result):
    covered: list[int] = []
    for regime in regimes(result["rows"]):
        assert regime["cap_lo"] <= regime["cap_hi"]
        covered.extend(range(regime["cap_lo"], regime["cap_hi"] + 1))
    assert covered == [row["cap"] for row in result["rows"]]


def test_regimes_merge_only_identical_rows(result):
    for regime in regimes(result["rows"]):
        span = [
            row
            for row in result["rows"]
            if regime["cap_lo"] <= row["cap"] <= regime["cap_hi"]
        ]
        first = span[0]
        for row in span:
            for key in ("proposed", "tp", "fp", "fn", "precision", "recall", "abstained"):
                assert row[key] == first[key]


def test_render_marks_the_derived_cap_and_the_naive_baseline(result):
    text = render_markdown(result)
    assert f"Derived cap: {result['derived_cap']}" in text
    assert "none (naive)" in text
    assert "singletons abstained" in text
    assert "| cap |" in text


def test_empty_cap_list_is_refused(farm):
    with pytest.raises(ValueError):
        sweep(farm, caps=[])


def test_score_threshold_is_passed_through(farm):
    """A high threshold must reduce links; the sweep must not silently ignore it."""
    low = sweep(farm, caps=[3], score_threshold=0.0)["rows"][0]
    high = sweep(farm, caps=[3], score_threshold=0.99)["rows"][0]
    assert high["proposed"] <= low["proposed"]
