"""Tests for the evaluation harness.

Each test pins a property that a reported number depends on. If one fails, some figure
in the results document has stopped meaning what it claims to mean -- most often by
becoming easier to satisfy.
"""

from __future__ import annotations

import pytest

from eval.metrics import (
    hit_at_k,
    mean_reciprocal_rank,
    precision_at_1,
    proportion,
    recall_at_k,
    score_abstention,
    score_links,
    wilson_interval,
)
from eval.splits import choose_threshold, cluster_split
from farm.generator import generate_farm
from farm.model import FarmConfig


@pytest.fixture(scope="session")
def farm():
    return generate_farm()


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #


def test_wilson_interval_stays_inside_the_unit_range():
    """The reason Wilson is used rather than the normal approximation."""
    for successes, total in ((0, 5), (5, 5), (1, 100), (99, 100), (0, 1)):
        low, high = wilson_interval(successes, total)
        assert 0.0 <= low <= high <= 1.0, (successes, total, low, high)


def test_wilson_interval_brackets_the_point_estimate():
    low, high = wilson_interval(7, 10)
    assert low < 0.7 < high


def test_wilson_interval_narrows_with_more_data():
    narrow = wilson_interval(70, 100)
    wide = wilson_interval(7, 10)
    assert (narrow[1] - narrow[0]) < (wide[1] - wide[0])


def test_proportion_of_nothing_is_zero_not_an_error():
    result = proportion(0, 0)
    assert result.value == 0.0
    assert result.total == 0


def test_precision_at_1_counts_only_the_top_ranked_candidate():
    rankings = [["a", "b"], ["c", "d"], ["e", "f"]]
    relevant = [{"a"}, {"d"}, {"e"}]
    result = precision_at_1(rankings, relevant)
    assert result.successes == 2
    assert result.total == 3


def test_hit_at_k_is_monotonic_in_k():
    rankings = [["x", "a"], ["y", "z", "b"]]
    relevant = [{"a"}, {"b"}]
    at_1 = hit_at_k(rankings, relevant, 1).successes
    at_2 = hit_at_k(rankings, relevant, 2).successes
    at_3 = hit_at_k(rankings, relevant, 3).successes
    assert at_1 <= at_2 <= at_3
    assert (at_1, at_2, at_3) == (0, 1, 2)


def test_recall_at_k_is_multi_positive_aware():
    """Correlation queries always have more than one right answer, so this matters."""
    rankings = [["a", "b", "c"]]
    relevant = [{"a", "b", "z"}]
    assert recall_at_k(rankings, relevant, 3) == pytest.approx(2 / 3)


def test_mean_reciprocal_rank_rewards_higher_positions():
    top = mean_reciprocal_rank([["a", "b"]], [{"a"}])
    second = mean_reciprocal_rank([["b", "a"]], [{"a"}])
    assert top == pytest.approx(1.0)
    assert second == pytest.approx(0.5)


def test_empty_queries_are_skipped_not_counted_as_failures():
    """A query with no candidate returned is an abstention, not a wrong answer.

    Only when no right answer exists, though -- see the test below. Here `{"z"}` is
    unreachable from an empty ranking, so this query is scored by `score_abstention`
    rather than counted as a miss.
    """
    result = precision_at_1([[], ["a"]], [set(), {"a"}])
    assert result.total == 1
    assert result.successes == 1
    assert result.skipped_no_answer == 1


def test_abstaining_where_an_answer_exists_is_a_miss_not_a_dropped_query():
    """Restraint is only correct when there was nothing to find.

    Part E1's table defines abstention as correct for "services with no cluster-mate
    present". A service that *does* have a cluster-mate and returns nothing has
    missed it. The first version dropped every empty ranking from the denominator,
    so a system that declined every hard query and answered one easy one reported
    1.00 over n=1 -- an inflated score whose n depended on the system's own
    behaviour, making two variants incomparable over the same queries.
    """
    # Four queries where a right answer existed and the system said nothing.
    result = precision_at_1([[], [], [], [], ["a"]], [{"a"}, {"b"}, {"c"}, {"d"}, {"a"}])
    assert result.total == 5, "every query with an answer must stay in the denominator"
    assert result.successes == 1
    assert result.abstained == 4
    assert result.value == pytest.approx(0.2), "1 of 5, not 1 of 1"

    # Overstating restraint must not look better than answering badly.
    honest_miss = precision_at_1([["z"], ["z"], ["z"], ["z"], ["a"]],
                                 [{"a"}, {"b"}, {"c"}, {"d"}, {"a"}])
    assert honest_miss.value == pytest.approx(result.value)


def test_a_system_that_abstains_on_everything_scores_zero_not_perfect():
    """The degenerate strategy the old denominator made look flawless."""
    result = precision_at_1([[], [], [], []], [{"a"}, {"b"}, {"c"}, {"d"}])
    assert result.value == 0.0
    assert result.total == 4
    assert result.abstained == 4


# --------------------------------------------------------------------------- #
# Link scoring -- denominators
# --------------------------------------------------------------------------- #


def test_false_merge_rate_is_the_complement_of_precision():
    """Part E2's central arithmetic: stating both targets separately is a contradiction."""
    scoring = score_links(
        proposed=[("a", "b"), ("c", "d")],
        truth=[("a", "b")],
    )
    assert scoring.false_merge_rate == pytest.approx(1.0 - scoring.precision)


def test_score_links_counts_each_outcome():
    scoring = score_links(
        proposed=[("a", "b"), ("c", "d")],
        truth=[("a", "b"), ("e", "f")],
    )
    assert (scoring.tp, scoring.fp, scoring.fn) == (1, 1, 1)


def test_score_links_accepts_either_pair_ordering():
    scoring = score_links(proposed=[("b", "a")], truth=[("a", "b")])
    assert scoring.tp == 1


def test_score_links_uses_the_stated_universe_for_true_negatives():
    scoring = score_links(
        proposed=[("a", "b")],
        truth=[("a", "b")],
        universe=[("a", "b"), ("a", "c"), ("b", "c")],
    )
    assert scoring.tn == 2


def test_precision_with_no_proposals_is_zero_not_one():
    """Proposing nothing must never read as perfect precision."""
    scoring = score_links(proposed=[], truth=[("a", "b")])
    assert scoring.precision == 0.0
    assert scoring.recall == 0.0


# --------------------------------------------------------------------------- #
# Abstention
# --------------------------------------------------------------------------- #


def test_score_abstention_counts_assertions_as_failures():
    result = score_abstention([False, False, True])
    assert result.correct_abstentions == 2
    assert result.false_assertions == 1


def test_always_abstaining_looks_perfect_on_abstention_alone():
    """Documented rather than hidden: abstention must be read next to recall.

    A system that never answers scores 100% here. That is exactly why the results
    document reports abstention beside the scoring metrics rather than instead of them.
    """
    result = score_abstention([False] * 10)
    assert result.abstention_rate == 1.0


# --------------------------------------------------------------------------- #
# Splits and threshold freezing
# --------------------------------------------------------------------------- #


def test_cluster_split_is_disjoint_and_complete(farm):
    split = cluster_split(farm)
    dev, test = set(split.dev_clusters), set(split.test_clusters)
    assert not (dev & test), "a cluster appears in both splits: the split leaks"
    assert dev | test == {c.cluster_id for c in farm.clusters}


def test_cluster_split_leaves_something_held_out(farm):
    """A split that fits and evaluates on the same clusters measures memorisation."""
    split = cluster_split(farm)
    assert split.test_clusters, "nothing held out"


def test_cluster_split_is_deterministic(farm):
    assert cluster_split(farm).dev_clusters == cluster_split(farm).dev_clusters


def test_single_cluster_farm_reports_a_degenerate_split():
    """Rather than silently evaluating on the fitting data."""
    tiny = generate_farm(FarmConfig(n_clusters=1, services_per_cluster=3))
    split = cluster_split(tiny)
    assert split.test_clusters == ()
    assert "degenerate" in split.name


def test_choose_threshold_picks_the_lowest_cut_meeting_the_floor():
    """Lowest cut means highest recall subject to the precision constraint."""
    points = [(0.9, True), (0.8, True), (0.5, False), (0.4, True)]
    decision = choose_threshold("t", points, min_precision=0.95)
    assert decision.value == pytest.approx(0.8)
    assert decision.dev_precision == pytest.approx(1.0)


def test_choose_threshold_abstains_when_the_floor_is_unachievable():
    """A refusal must be a threshold the system can fail to reach, not a silent pass."""
    points = [(0.9, False), (0.8, True), (0.7, False)]
    decision = choose_threshold("t", points, min_precision=0.95)
    assert decision.value > max(score for score, _ in points)
    assert decision.dev_n == 0
    assert "unachievable" in decision.policy


def test_choose_threshold_handles_no_dev_data():
    decision = choose_threshold("t", [], min_precision=0.95)
    assert decision.dev_n == 0
    assert decision.value == 1.0


def test_thresholds_record_that_they_were_fitted_on_dev():
    decision = choose_threshold("t", [(0.9, True)], min_precision=0.5)
    assert decision.fitted_on == "dev"
    assert decision.to_dict()["fitted_on"] == "dev"


# --------------------------------------------------------------------------- #
# Detectors: the separation each one exists to demonstrate
# --------------------------------------------------------------------------- #


def _correlation_link_scoring(farm, cap):
    from eval.detectors import correlation
    from eval.metrics import score_links

    proposed = []
    for service in farm.services:
        if not service.cluster_id:
            continue
        for candidate, _score in correlation.rank_services(
            farm,
            service.service_id,
            farm.correlation_pool(service.service_id),
            max_artifact_frequency=cap,
        ):
            proposed.append((service.service_id, candidate))

    truth = [
        (s.service_id, mate)
        for s in farm.services
        if s.cluster_id
        for mate in farm.cluster_mates(s.service_id)
    ]
    return score_links(proposed, truth).to_dict()


def test_careful_rules_beat_naive_rules_on_precision(farm):
    """If the negative fixtures could not fail a careless rule set they would be
    decoration, and the careful set's perfect precision would mean nothing."""
    from eval.detectors import misconfig

    careful = misconfig.evaluate(farm, misconfig.RULES)
    naive = misconfig.evaluate(farm, misconfig.NAIVE_RULES)
    assert careful.precision > naive.precision
    assert naive.fp > 0, "the naive rule set produced no false positives at all"


def test_careful_rules_find_the_injected_flaws_without_false_positives(farm):
    from eval.detectors import misconfig

    scoring = misconfig.evaluate(farm, misconfig.RULES)
    assert scoring.recall >= 0.90, "Part E1 targets >90% recall"
    assert scoring.fp == 0, f"unexpected false positives: {scoring.false_positives}"


def test_frequency_filtering_improves_correlation_link_precision(farm):
    from eval.detectors import correlation

    cap = correlation.common_value_threshold(farm.config.services_per_cluster)
    naive = _correlation_link_scoring(farm, None)
    filtered = _correlation_link_scoring(farm, cap)
    assert filtered["precision"] > naive["precision"]
    assert filtered["false_merge_rate"] < naive["false_merge_rate"]
    assert naive["false_merge_rate"] > 0.0, "the naive correlator was not fooled at all"


def test_placeholder_blocklist_removes_decoy_false_merges(farm):
    """The entity-resolution negative class must actually be defeated by the blocklist."""
    from eval.detectors import entity

    unblocked = entity.evaluate(farm, block_placeholder=False)
    blocked = entity.evaluate(farm, block_placeholder=True)
    assert unblocked["false_merges_on_decoys"] > 0, "the placeholder caught nothing"
    assert blocked["false_merges_on_decoys"] == 0
    assert blocked["links"]["precision"] > unblocked["links"]["precision"]
    assert blocked["links"]["recall"] == unblocked["links"]["recall"], (
        "the blocklist should cost nothing in recall on this farm"
    )


def test_exact_match_entity_resolution_leaks_false_merges(farm):
    """Documents the failure the blocklist fixes rather than assuming it."""
    from eval.detectors import entity

    result = entity.evaluate(farm, block_placeholder=False)
    assert result["links"]["false_merge_rate"] > 0.0
    assert result["decoy_false_merge_rate"] == 1.0, (
        "every decoy pair should be falsely merged by an exact-match rule"
    )


def test_entity_resolution_never_asserts_identity(farm):
    """Part E2.5: the strongest available string is a candidate link with evidence."""
    from eval.detectors import entity

    for link in entity.propose_merges(farm):
        assert link.evidence, "a candidate link without evidence is not reportable"
        assert 0.0 <= link.score <= 1.0


# --------------------------------------------------------------------------- #
# Attribution
# --------------------------------------------------------------------------- #


def test_attribution_queries_never_leak_into_their_own_profile(farm):
    """The query half must be disjoint from the profile half, or the score is memorisation."""
    from eval.detectors import attribution

    for query in attribution.build_queries(farm):
        profile = attribution.profile_posts(farm, query.persona_id, query)
        profile_ids = {p.post_id for p in profile}
        query_ids = {p.post_id for p in query.posts}
        assert not (profile_ids & query_ids), query.persona_id
        assert profile_ids or query_ids, query.persona_id


def test_attribution_reports_every_model(farm):
    from eval.detectors import attribution

    results = attribution.evaluate(farm)
    assert set(results["models"]) == set(attribution.MODELS)
    for entry in results["models"].values():
        for bucket in ("long", "short"):
            assert entry[bucket]["random_baseline"]["hit@3"] > 0.0


def test_attribution_states_its_scope_limit(farm):
    """The synthetic-retrieval caveat must travel with the numbers."""
    from eval.detectors import attribution

    scope = attribution.evaluate(farm)["scope_limit"]
    assert "synthetic" in scope.lower()
    assert "not validated" in scope.lower()


def test_attribution_behavioural_beats_lexical_on_short_posts(farm):
    """The plan's E3 hypothesis, measured rather than assumed.

    If this ever stops holding, the E3 claim in the plan is wrong for this farm and the
    report must say so -- which is exactly why it is a test and not a sentence.
    """
    from eval.detectors import attribution

    results = attribution.evaluate(farm)["models"]
    behaviour = results["behaviour"]["short"]["hit@3"]["value"]
    text_full = results["text_full"]["short"]["hit@3"]["value"]
    assert behaviour > text_full, (
        f"behaviour {behaviour} did not beat bag-of-words {text_full} on short posts"
    )


def test_function_words_beat_content_words_on_long_posts(farm):
    """Function words are the drift-robust signal; content words are not."""
    from eval.detectors import attribution

    results = attribution.evaluate(farm)["models"]
    assert (
        results["text_function"]["long"]["recall@5"]
        >= results["text_full"]["long"]["recall@5"]
    )


def test_attribution_models_are_actually_different(farm):
    """If every ablation produced identical numbers the ablation would be measuring nothing."""
    from eval.detectors import attribution

    results = attribution.evaluate(farm)["models"]
    short_scores = {
        model: entry["short"]["hit@3"]["value"] for model, entry in results.items()
    }
    assert len(set(short_scores.values())) > 1, f"all models identical: {short_scores}"


def test_drift_buckets_partition_the_queries(farm):
    """Every query lands in exactly one bucket, and a control group exists."""
    from eval.detectors import attribution

    queries = attribution.build_queries(farm)
    drifted = [q for q in queries if q.drifted]
    stable = [q for q in queries if not q.drifted]
    assert len(drifted) + len(stable) == len(queries)
    assert drifted, "no post-drift query, so nothing tests robustness to drift"
    assert stable, (
        "no pre-drift query, so the drift comparison has no control group and a model "
        "that is simply weak everywhere would look drift-robust"
    )


def test_drift_labels_come_from_the_farm_not_from_query_size(farm):
    """Regression: the label was once `len(posts) > 4`, a post-count proxy.

    That made one bucket an artefact of how long the held-out queries happened to be, so
    the table read as a finding about drift while reporting something else. The label is
    now the farm's recorded boundary, which must agree with it exactly.
    """
    from eval.detectors import attribution

    for query in attribution.build_queries(farm):
        persona = next(p for p in farm.personas if p.persona_id == query.persona_id)
        expected = persona.drift_at is not None and any(
            post.index >= persona.drift_at for post in query.posts
        )
        assert query.drifted is expected, query.persona_id


def test_drift_separates_content_words_from_function_words(farm):
    """The D3 claim, as a crossover rather than a one-sided result.

    A single-sided claim ("function words survive drift") cannot tell drift robustness
    apart from a model that is weak everywhere. Asserting both directions can: content
    words must win when nothing drifted, and lose when something did.
    """
    from eval.detectors import attribution

    models = attribution.evaluate(farm)["models"]

    def score(model, bucket):
        key = "drifted_corpus_queries" if bucket == "drifted" else "stable_corpus_queries"
        return models[model]["drift_breakdown"][key]["hit@3"]["value"]

    assert score("text_function", "drifted") > score("text_full", "drifted"), (
        "after drift, the content-word model did not do worse than the function-word "
        "model, so this farm does not demonstrate D3's drift robustness claim"
    )
    assert score("text_full", "stable") > score("text_function", "stable"), (
        "before drift, the content-word model did not beat the function-word model, so "
        "the drift split is not isolating the effect it claims to isolate"
    )


def test_drift_bucket_totals_account_for_every_query(farm):
    """Denominators must add up, or the split is silently dropping queries."""
    from eval.detectors import attribution

    queries = attribution.build_queries(farm)
    models = attribution.evaluate(farm)["models"]
    for model, entry in models.items():
        breakdown = entry["drift_breakdown"]
        total = (
            breakdown["drifted_corpus_queries"]["hit@3"]["total"]
            + breakdown["stable_corpus_queries"]["hit@3"]["total"]
        )
        assert total == len(queries), f"{model}: {total} of {len(queries)} queries scored"


# --------------------------------------------------------------------------- #
# Provenance
# --------------------------------------------------------------------------- #


def test_ledger_verifies_on_an_untouched_farm(farm):
    from eval.detectors import provenance

    ledger = provenance.build_ledger(farm)
    assert provenance.verify_ledger(farm, ledger)["verified"] is True


def test_tamper_self_test_detects_a_mutation_and_restores(farm):
    """A provenance mechanism never shown to reject anything has not been shown to work."""
    from eval.detectors import provenance

    ledger = provenance.build_ledger(farm)
    result = provenance.tamper_detection_self_test(farm, ledger)
    assert result["runs"] is True
    assert result["detected"] is True
    assert result["clean_after_restore"] is True, "the self-test left the farm mutated"


def test_status_code_is_folded_into_the_digest(farm):
    """A 404 and a 200 with the same body are different observations."""
    from farm.model import Endpoint
    from eval.detectors import provenance

    body = "same bytes"
    assert provenance.snapshot_digest(Endpoint(path="/x", body=body, status=200)) != (
        provenance.snapshot_digest(Endpoint(path="/x", body=body, status=404))
    )


def test_claim_resolution_finds_the_source_snapshot(farm):
    from eval.detectors import provenance

    ledger = provenance.build_ledger(farm)
    service = farm.services[0]
    endpoint = service.endpoints[0]
    result = provenance.resolve_claim(farm, ledger, service.service_id, endpoint.path)
    assert result["resolved"] is True


def test_claim_resolution_fails_loudly_for_an_unknown_claim(farm):
    """An unresolvable claim must not silently resolve to a plausible neighbour."""
    from eval.detectors import provenance

    ledger = provenance.build_ledger(farm)
    result = provenance.resolve_claim(farm, ledger, "svc-does-not-exist", "/nope")
    assert result["resolved"] is False
    assert "reason" in result


# --------------------------------------------------------------------------- #
# Harness end to end
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="session")
def results():
    from eval import harness

    return harness.run(FarmConfig(seed=20260925))


def test_harness_reports_every_module(results):
    assert set(results["modules"]) == {
        "M1_collection",
        "M2_misconfig_rules",
        "M2_correlation",
        "M3_entity_resolution",
        "M5_attribution",
        "E4_provenance",
    }


def test_m1_collection_is_scored_on_the_fixture_seam(results):
    """The collector exists, so the report must score its seam, not announce its absence."""
    module = results["modules"]["M1_collection"]

    assert module["status"] == "fixture-verified"
    assert module["passed"] is True, module["notes"]
    for name in (
        "scope_refused_unsent",
        "second_run_idempotent",
        "checkpoints_saved",
        "warc_records_linked",
        "no_self_seeding",
    ):
        assert module["checks"].get(name), (name, module["notes"])
    assert module["checks"]["first_run_stored"] > 0


def test_m1_report_states_the_fixture_limit_in_words(results):
    """A scored M1 without the fixture caveat would upgrade the number."""
    from eval import report

    rendered = report.render(results)

    assert "M1 collection integrity" in rendered
    assert "fixture-verified" in rendered
    assert "does not by itself mean live collection succeeds" in rendered
    assert "eight-service lab" in rendered


def test_every_threshold_records_the_split_it_was_fitted_on(results):
    """Part E2: thresholds are frozen on dev before test is scored."""
    assert results["thresholds"], "no thresholds frozen at all"
    for decision in results["thresholds"]:
        assert decision["fitted_on"] == "dev", decision["name"]
        assert "dev_precision" in decision, decision["name"]
        assert "dev_n" in decision, decision["name"]


def test_thresholds_are_frozen_not_fitted_on_the_scored_data(results):
    """The dev fit must be derivable from dev alone.

    `M2_correlation.at_frozen_threshold` is the number that would differ had the
    threshold been chosen on test -- which is the whole reason it is recorded.
    """
    correlation = results["modules"]["M2_correlation"]
    assert "at_frozen_threshold" in correlation
    assert "frozen_link_threshold" in correlation
    assert correlation["frozen_link_threshold"]["fitted_on"] == "dev"


def test_abstention_is_scored_both_ways(results):
    """Both are reported because the unthresholded figure is the honest baseline."""
    assert "retrieval_unthresholded" in results["abstention"]
    assert "retrieval_at_frozen_threshold" in results["abstention"]


def test_frozen_threshold_reduces_false_assertions(results):
    """The point of applying a threshold at all."""
    unthresholded = results["abstention"]["retrieval_unthresholded"]["scoring"]
    thresholded = results["abstention"]["retrieval_at_frozen_threshold"]["scoring"]
    assert thresholded["false_assertions"] <= unthresholded["false_assertions"]
    assert unthresholded["false_assertions"] > 0, (
        "no false assertions to fix means the threshold is being credited for nothing"
    )


def test_every_rate_carries_its_denominator(results):
    """A percentage without a denominator is not a result."""

    def is_threshold_decision(node):
        # `value` here is a cut point chosen on dev, not a measured rate.
        return {"policy", "fitted_on"} <= set(node)

    def walk(node, path="results"):
        if isinstance(node, dict):
            if (
                "value" in node
                and isinstance(node["value"], float)
                and not is_threshold_decision(node)
            ):
                assert "total" in node, f"{path}: a rate without a denominator"
            for key, child in node.items():
                walk(child, f"{path}.{key}")
        elif isinstance(node, list):
            for index, child in enumerate(node):
                walk(child, f"{path}[{index}]")

    walk(results)


def test_harness_carries_its_limitations(results):
    """Part E5: the limitations must travel with the numbers, not beside them."""
    assert len(results["limitations"]) >= 5
    joined = " ".join(results["limitations"]).lower()
    assert "not live" in joined or "synthetic" in joined


def test_results_are_json_serialisable(results):
    import json

    round_tripped = json.loads(json.dumps(results))
    assert round_tripped["meta"]["farm_fingerprint"] == results["meta"]["farm_fingerprint"]


def test_harness_is_reproducible_from_the_seed(results):
    """The whole point of a deterministic farm: same seed, same numbers."""
    import json

    from eval import harness

    again = harness.run(FarmConfig(seed=20260925))
    assert json.dumps(again, sort_keys=True) == json.dumps(results, sort_keys=True)


def test_a_different_seed_changes_the_numbers(results):
    """Guards against a harness that silently ignores its seed."""
    from eval import harness

    other = harness.run(FarmConfig(seed=999))
    assert other["meta"]["farm_fingerprint"] != results["meta"]["farm_fingerprint"]


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #


def test_report_covers_every_module(results):
    from eval import report

    rendered = report.render(results)
    for heading in (
        "## Held-out split",
        "## Frozen thresholds",
        "## M2 — Misconfiguration rules",
        "## M2 — Cross-service correlation",
        "## M3 — Entity resolution",
        "## M5 — Attribution",
        "## E4 — Provenance",
        "## Abstention",
        "## Stated limitations",
    ):
        assert heading in rendered, heading


def test_report_states_the_seed_and_command(results):
    """Reproducibility information belongs in the report, not in a footnote elsewhere."""
    from eval import report

    rendered = report.render(results)
    assert str(results["meta"]["seed"]) in rendered
    assert "python -m eval run" in rendered
    assert results["meta"]["farm_fingerprint"] in rendered


def test_report_shows_baselines_next_to_scores(results):
    """The report is required to put the random baseline beside every score."""
    from eval import report

    rendered = report.render(results)
    assert "random" in rendered.lower()


def test_cli_writes_results_and_report(tmp_path):
    from eval.cli import main

    code = main(["run", "--seed", "4242", "--out", str(tmp_path)])
    assert code == 0
    assert (tmp_path / "eval-results.json").exists()
    assert (tmp_path / "eval-report.md").exists()

# --------------------------------------------------------------------------- #
# The reported baseline must describe the pool that was actually ranked
# --------------------------------------------------------------------------- #


def test_correlation_baseline_describes_the_pool_actually_ranked(farm):
    """The baseline next to a result must match the experiment that produced it.

    The harness ranked against the full pool (singletons included) but reported the
    *restricted* baseline, 2/11 = 0.1818. Part E1's rule is that a random baseline
    sits next to every metric; a baseline computed over a pool the run did not use is
    a number that describes a different experiment, which is worse than no baseline
    because it looks like a control.
    """
    from eval.detectors import correlation

    service = next(s for s in farm.services if s.cluster_id)
    pool = farm.correlation_pool(service.service_id)
    ranked = correlation.rank_services(farm, service.service_id, pool)

    # The pool actually handed to the ranker is the full one.
    assert len(pool) == len(farm.services) - 1 == 13
    assert any("solo" in candidate for candidate in pool)
    assert ranked and all(candidate in pool for candidate, _ in ranked)

    # So the baseline reported must be the full-pool one.
    from eval.harness import correlation_experiment

    scores = correlation_experiment(farm, max_artifact_frequency=None)
    reported = scores["ranking"]["random_baseline_precision@1"]
    assert reported == pytest.approx(2 / 13), (
        "reported baseline must match the 13-candidate pool the ranker was given"
    )
    assert scores["ranking"]["random_baseline_precision@1_restricted"] == pytest.approx(2 / 11)
    assert "pool actually ranked" in scores["ranking"]["baseline_note"]


def test_the_reported_baseline_is_strictly_below_the_measured_score(farm):
    """A baseline above the result would mean the system is worse than chance.

    Cheaper as a live check than reading the table: if the two ever cross, either the
    correlator regressed or the baseline is describing the wrong pool.
    """
    from eval.harness import correlation_experiment

    scores = correlation_experiment(farm, max_artifact_frequency=3)
    ranking = scores["ranking"]
    assert ranking["precision@1"]["value"] > ranking["random_baseline_precision@1"]


# --------------------------------------------------------------------------- #
# The split has to be used, not merely declared
# --------------------------------------------------------------------------- #


def test_the_held_out_block_scores_only_test_clusters(results):
    """A split nobody scores on is a heading, not a held-out set.

    The module tables cover the whole farm, so the report needs at least one block where
    every scored service came from the test side of the split.
    """
    held_out = results["held_out"]
    test_clusters = set(results["split"]["test_clusters"])

    assert held_out["clusters"] == list(results["split"]["test_clusters"])
    assert test_clusters, "a split with no test clusters holds nothing out"
    assert held_out["query_count"] == held_out["service_count"]

    # Every misconfiguration truth item, detection and miss is a test service.
    rules = held_out["M2_misconfig_rules"]["careful_rules"]
    for entry in (rules["missed"], rules["false_positives"]):
        for item in entry:
            assert item.split(":")[0] in _services_in_clusters(results, test_clusters)

    # The ranking pool is the test services alone, so the baseline is 2/(n-1) not 2/13.
    ranking = held_out["M2_correlation"]["at_frozen_threshold"]["ranking"]
    assert ranking["pool_size"] == held_out["service_count"] - 1
    assert ranking["n_queries"] == held_out["query_count"]
    mates = results["meta"]["farm_config"]["services_per_cluster"] - 1
    assert ranking["random_baseline_precision@1"] == pytest.approx(
        mates / (held_out["service_count"] - 1)
    ), "the held-out baseline must be the one for the held-out pool"


def _services_in_clusters(results, cluster_ids):
    """The service ids the farm placed in these clusters, rebuilt from the recorded config."""
    from farm.generator import generate_farm
    from farm.model import FarmConfig

    farm = generate_farm(FarmConfig(**results["meta"]["farm_config"]))
    return {s.service_id for s in farm.services if s.cluster_id in cluster_ids}


def test_a_held_out_pool_contains_no_dev_service(farm):
    """The defect this guards: a test query answered by a dev service is not held out."""
    from eval.splits import cluster_split

    split = cluster_split(farm)
    dev = {s.service_id for s in farm.services if split.is_dev(s.cluster_id)}
    test = {s.service_id for s in farm.services if split.is_test(s.cluster_id)}

    for service_id in sorted(test):
        pool = farm.correlation_pool(service_id, service_ids=test)
        assert pool and not (set(pool) & dev)
        assert service_id not in pool
    baseline = farm.precision_at_1_baseline(service_ids=test)
    expected = len(farm.cluster_mates(sorted(test)[0])) / (len(test) - 1)
    assert baseline == pytest.approx(expected), (
        "the held-out baseline must describe the held-out pool"
    )


def test_the_limitations_do_not_claim_what_the_run_does_not_do(results):
    """The sentence this replaces said the test split made evaluation held out.

    It did not: the module tables scored the whole farm. A limitation that flatters the
    method is the one kind of error the report exists to prevent.
    """
    joined = " ".join(results["limitations"]).lower()

    assert "on unseen operators by construction" not in joined
    assert "only the `held_out` block" in joined
    assert "module tables score the whole farm" in joined


def test_the_report_prints_the_held_out_numbers_beside_the_full_farm(results):
    from eval import report

    rendered = report.render(results)
    section = rendered.split("## Held-out split", 1)[1].split("## Frozen thresholds", 1)[0]

    assert "Measured on test clusters only" in section
    assert "Correlation precision@1 (frozen threshold)" in section

    held = results["held_out"]["M2_correlation"]["at_frozen_threshold"]["ranking"]["precision@1"]
    whole = results["modules"]["M2_correlation"]["at_frozen_threshold"]["ranking"]["precision@1"]
    # Each column carries its own denominator, so neither can be read as the other.
    assert f"({held['successes']}/{held['total']})" in section
    assert f"({whole['successes']}/{whole['total']})" in section
    assert held["total"] != whole["total"], "both columns describe the same experiment"
