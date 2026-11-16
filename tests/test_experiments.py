"""Tests for the secondary experiments.

The experiments are not decorative: the paper's drift section now depends on the
paired design, and the sweeps are the evidence that the correlation result is not one
lucky topology. These tests pin the design properties that make those claims valid.
"""

from __future__ import annotations

from eval.experiments import (
    _paired_farm,
    cap_ablation,
    mcnemar_exact,
    paired_drift,
    seed_sweep,
)
from farm.model import StyleRegime


def test_mcnemar_exact_matches_hand_computed_values():
    # Three discordant pairs, all one way: 2 * (1/8) = 0.25.
    assert mcnemar_exact(3, 0) == 0.25
    assert mcnemar_exact(0, 3) == 0.25
    # Four discordant pairs split three and one: 2 * (1 + 4) / 16 = 0.625.
    assert mcnemar_exact(3, 1) == 0.625
    # A two-and-two split is the null's centre, so the exact p is 1.0.
    assert mcnemar_exact(2, 2) == 1.0
    assert mcnemar_exact(0, 0) == 1.0


def test_paired_farm_keeps_profiles_on_the_pre_drift_side():
    farm = _paired_farm(20260925, posts_per_persona=24, drift_at=12)
    for persona in farm.personas:
        if persona.style is StyleRegime.TEMPLATE_REUSER:
            assert persona.drift_at is None
            continue
        assert persona.drift_at == 12
        assert len(persona.posts) == 24
        # The first six posts are the profile the experiment fixes for every model.
        assert all(post.index < 12 for post in persona.posts[:6])


def test_paired_drift_reports_both_conditions_and_a_placebo():
    result = paired_drift(
        seed=20260925,
        posts_per_persona=16,
        drift_at=8,
        profile_posts=4,
        query_posts=4,
        models=("text_full", "text_function", "behaviour"),
    )
    assert result["design"]["drifted_personas"] == 16
    assert result["design"]["stable_personas"] == 4
    for model in ("text_full", "text_function", "behaviour"):
        entry = result["models"][model]
        assert entry["drifted"]["n_pairs"] == 16
        assert entry["stable_placebo"]["n_pairs"] == 4
        assert "delta_hit@3" in entry["drifted"]
        assert "mcnemar_hit@3" in entry["drifted"]
        assert len(entry["drifted"]["per_persona"]) == 16


def test_cap_ablation_marks_the_derived_cap_and_the_too_small_one():
    result = cap_ablation(seed=20260925)
    caps = {row["cap"]: row for row in result["rows"]}
    assert caps[3]["derived_cap"] is True
    assert caps[3]["precision"] == 1.0 and caps[3]["recall"] == 1.0
    # A cap below the true cluster size discards genuine cluster artefacts.
    assert caps[2]["recall"] < 1.0
    assert caps[None]["precision"] < 1.0


def test_seed_sweep_keeps_the_filtered_invariants():
    result = seed_sweep((20260925, 20260926))
    assert result["invariants"]["filtered_precision_all_one"] is True
    assert result["invariants"]["filtered_false_merge_all_zero"] is True
    assert result["summary"]["filtered_link_precision"]["n"] == 2
