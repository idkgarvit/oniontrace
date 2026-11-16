"""Experiments the headline evaluation cannot answer on its own.

The harness answers "what does the system score on this farm". A paper that stops
there has two weak points: the drift result compares one population of personas
with another, and the correlation result rests on one topology. Both are fixed
here.

Three designs live in this module:

  * ``paired_drift`` -- one profile per persona, two held-out queries from the same
    persona (before and after its recorded drift point). The comparison is
    within-persona, so a model that is weak everywhere cannot look drift-robust by
    being compared with a different, easier group.
  * ``seed_sweep`` / ``topology_sweep`` -- repeat the whole harness over seeds and
    over farm shapes, and report the distribution rather than one draw.
  * ``cap_ablation`` and ``correlation_paired_test`` -- show what the frequency cap
    does as a parameter, and test the naive-versus-filtered difference on per-query
    outcomes rather than on aggregate counts alone.

Everything is deterministic: the paired corpora are regenerated from a string seed
derived from the farm seed and persona id, and the bootstrap uses its own seed.

Run:  python3 scripts/run_experiments.py
"""

from __future__ import annotations

import hashlib
import math
import random
import statistics
from typing import Any, Iterable, Sequence

from eval.detectors import attribution, correlation
from eval.harness import correlation_experiment, run as harness_run
from farm.generator import generate_farm
from farm.model import FarmConfig, Persona, StyleRegime
from farm.personas import generate_posts

#: Default seed, so the paired experiment describes the same farm as the paper's
#: headline table. The other seeds are only used by the sweeps.
DEFAULT_SEED = 20260925

#: Seeds for the sweep. 20260925 is the headline; 20260926 and 20260927 were the
#: original three-seed check and are kept so the old claim can be compared.
SWEEP_SEEDS: tuple[int, ...] = (20260925,) + tuple(range(20260926, 20260945))

#: Farm shapes for the topology sweep: (clusters, services per cluster). The default
#: is (4, 3). Three seeds per shape is enough to see a topology break without turning
#: the sweep into the experiment it is checking.
TOPOLOGIES: tuple[tuple[int, int], ...] = (
    (3, 3),
    (4, 2),
    (4, 3),
    (4, 4),
    (5, 3),
    (6, 3),
)

MODELS: tuple[str, ...] = attribution.MODELS


# --------------------------------------------------------------------------- #
# Small statistics helpers
# --------------------------------------------------------------------------- #


def _seed_for(label: str) -> int:
    """A stable integer seed from a string. `hash()` would vary per process."""
    return int.from_bytes(hashlib.sha256(label.encode("utf-8")).digest()[:8], "big")


def mcnemar_exact(pre_only: int, post_only: int) -> float:
    """Two-sided exact McNemar p-value for matched binary outcomes.

    The usual chi-square approximation is not trustworthy at n=16, so this uses
    the exact binomial: under the null the discordant pairs are equally likely to
    fall either way. The p-value is two-sided.
    """
    discordant = pre_only + post_only
    if discordant == 0:
        return 1.0
    tail = sum(math.comb(discordant, i) for i in range(min(pre_only, post_only) + 1))
    return min(1.0, 2.0 * tail / (2**discordant))


def paired_bootstrap(
    before: Sequence[float],
    after: Sequence[float],
    *,
    repetitions: int = 10_000,
    seed: int = 20261001,
) -> dict[str, Any]:
    """Bootstrap CI for the mean within-pair change (after - before).

    Resampling pairs, not observations, keeps the pairing: the question is whether
    this persona changed, not whether two independent samples differ.
    """
    if len(before) != len(after) or not before:
        return {"delta": 0.0, "ci95": [0.0, 0.0], "n_pairs": 0}
    differences = [b - a for a, b in zip(before, after)]
    rng = random.Random(seed)
    n = len(differences)
    draws = sorted(
        sum(differences[rng.randrange(n)] for _ in range(n)) / n
        for _ in range(repetitions)
    )
    return {
        "delta": round(sum(differences) / n, 6),
        "ci95": [
            round(draws[int(0.025 * repetitions)], 6),
            round(draws[int(0.975 * repetitions) - 1], 6),
        ],
        "n_pairs": n,
    }


def _describe(values: Sequence[float]) -> dict[str, Any]:
    """Mean, sample standard deviation, min and max, rounded for the JSON."""
    if not values:
        return {"n": 0, "mean": None, "sd": None, "min": None, "max": None}
    return {
        "n": len(values),
        "mean": round(statistics.fmean(values), 6),
        "sd": round(statistics.stdev(values), 6) if len(values) > 1 else 0.0,
        "min": round(min(values), 6),
        "max": round(max(values), 6),
    }


# --------------------------------------------------------------------------- #
# Paired style-drift experiment
# --------------------------------------------------------------------------- #


def _paired_farm(
    seed: int,
    *,
    posts_per_persona: int,
    drift_at: int,
) -> Any:
    """Build the usual farm, then regenerate corpora long enough for a paired test.

    The default farm has twelve posts and a drift point at index four, which leaves
    no pre-drift posts to hold out. The generator's drift point is a constant, so the
    experiment regenerates the corpora with a longer run and a later boundary using
    the same persona objects. Only the corpora move: services, flaws, clusters,
    decoys and ground truth are the ones the harness built.
    """
    farm = generate_farm(FarmConfig(seed=seed))
    for persona in farm.personas:
        service_id = persona.posts[0].service_id if persona.posts else None
        drift = drift_at if persona.style is not StyleRegime.TEMPLATE_REUSER else None
        persona.drift_at = drift
        if drift is None:
            persona.drift_topic = None
        rng = random.Random(
            f"{seed}:paired-drift:{persona.persona_id}:{posts_per_persona}:{drift_at}"
        )
        persona.posts = generate_posts(
            rng,
            persona,
            service_id,
            post_count=posts_per_persona,
            drift_at=drift,
            drift_topic=persona.drift_topic,
        )
    return farm


def _profile_map(farm: Any, profile_posts: int) -> dict[str, list]:
    """The fixed candidate profiles: the first N posts of every persona.

    For a drifting persona those are pre-drift by construction, which is the point:
    the profile does not change between the two queries, so the only difference is
    whether the query text comes from before or after the vocabulary switch.
    """
    return {
        persona.persona_id: list(persona.posts[:profile_posts])
        for persona in farm.personas
    }


def _rank_with_profiles(
    query_posts: Sequence, profiles: dict[str, list], model: str
) -> list[tuple[str, float]]:
    """Rank fixed candidate profiles for one query under one model."""
    query_features = attribution.features_for(query_posts, model)
    ranked: list[tuple[str, float]] = []
    for persona_id, profile in profiles.items():
        score = attribution.cosine(
            query_features, attribution.features_for(profile, model)
        )
        if score > 0:
            ranked.append((persona_id, score))
    ranked.sort(key=lambda item: (-item[1], item[0]))
    return ranked


def _rank_of(ranked: Sequence[tuple[str, float]], truth: str) -> int | None:
    for position, (persona_id, _) in enumerate(ranked, 1):
        if persona_id == truth:
            return position
    return None


def _hit(rank: int | None, k: int) -> int:
    return int(rank is not None and rank <= k)


def _condition_summary(
    ranks: Sequence[int | None], truths: Sequence[str], n: int
) -> dict[str, Any]:
    return {
        "n": len(truths),
        "hit@1": round(sum(_hit(rank, 1) for rank in ranks) / n, 6) if n else 0.0,
        "hit@3": round(sum(_hit(rank, 3) for rank in ranks) / n, 6) if n else 0.0,
        "mrr": round(
            sum(1.0 / rank for rank in ranks if rank is not None) / n, 6
        )
        if n
        else 0.0,
    }


def _paired_summary(
    rows: Sequence[dict[str, Any]],
    truth_key: str,
    *,
    bootstrap_seed: int,
) -> dict[str, Any]:
    """Summarise one model's pre/post outcomes over the same personas."""
    n = len(rows)
    if not n:
        return {"n_pairs": 0}
    pre_ranks = [row["pre_rank"] for row in rows]
    post_ranks = [row["post_rank"] for row in rows]

    summary: dict[str, Any] = {
        "n_pairs": n,
        "pre": _condition_summary(pre_ranks, [row[truth_key] for row in rows], n),
        "post": _condition_summary(post_ranks, [row[truth_key] for row in rows], n),
    }
    for k in (1, 3):
        pre_hits = [_hit(rank, k) for rank in pre_ranks]
        post_hits = [_hit(rank, k) for rank in post_ranks]
        pre_only = sum(1 for a, b in zip(pre_hits, post_hits) if a and not b)
        post_only = sum(1 for a, b in zip(pre_hits, post_hits) if b and not a)
        summary[f"delta_hit@{k}"] = paired_bootstrap(
            pre_hits, post_hits, seed=bootstrap_seed + k
        )
        summary[f"mcnemar_hit@{k}"] = {
            "pre_only": pre_only,
            "post_only": post_only,
            "p_exact": round(mcnemar_exact(pre_only, post_only), 6),
        }
    summary["per_persona"] = [
        {
            "persona_id": row["persona_id"],
            "style": row["style"],
            "pre_rank": row["pre_rank"],
            "post_rank": row["post_rank"],
        }
        for row in rows
    ]
    return summary


def paired_drift(
    seed: int = DEFAULT_SEED,
    *,
    posts_per_persona: int = 24,
    drift_at: int = 12,
    profile_posts: int = 6,
    query_posts: int = 6,
    models: Sequence[str] = MODELS,
) -> dict[str, Any]:
    """Within-persona drift test: same profile, pre-drift query vs post-drift query.

    For every drifting persona the profile is posts ``[0, profile_posts)``, the
    pre-drift query is ``[profile_posts, drift_at)``, and the post-drift query is
    ``[drift_at, drift_at + query_posts)``. The two queries are the same size by
    construction, so a hit-rate change cannot be a query-length artefact.

    Template reusers do not drift. They form a placebo control: the same two
    windows are scored, and a change there is time-in-corpus noise rather than a
    response to drift.
    """
    farm = _paired_farm(
        seed, posts_per_persona=posts_per_persona, drift_at=drift_at
    )
    profiles = _profile_map(farm, profile_posts)
    pool = sorted(profiles)

    drifters = [
        persona
        for persona in farm.personas
        if persona.style is not StyleRegime.TEMPLATE_REUSER
        and len(persona.posts) >= drift_at + query_posts
    ]
    stable = [
        persona
        for persona in farm.personas
        if persona.style is StyleRegime.TEMPLATE_REUSER
        and len(persona.posts) >= profile_posts + 2 * query_posts
    ]

    results: dict[str, Any] = {}
    for model in models:
        rows: list[dict[str, Any]] = []
        for persona in drifters:
            pre = persona.posts[profile_posts:drift_at]
            post = persona.posts[drift_at : drift_at + query_posts]
            pre_rank = _rank_of(
                _rank_with_profiles(pre, profiles, model), persona.persona_id
            )
            post_rank = _rank_of(
                _rank_with_profiles(post, profiles, model), persona.persona_id
            )
            rows.append(
                {
                    "persona_id": persona.persona_id,
                    "style": persona.style.value,
                    "truth": persona.persona_id,
                    "pre_rank": pre_rank,
                    "post_rank": post_rank,
                    "pre_words": sum(p.word_count for p in pre),
                    "post_words": sum(p.word_count for p in post),
                }
            )

        controls: list[dict[str, Any]] = []
        for persona in stable:
            pre = persona.posts[profile_posts : profile_posts + query_posts]
            post = persona.posts[
                profile_posts + query_posts : profile_posts + 2 * query_posts
            ]
            pre_rank = _rank_of(
                _rank_with_profiles(pre, profiles, model), persona.persona_id
            )
            post_rank = _rank_of(
                _rank_with_profiles(post, profiles, model), persona.persona_id
            )
            controls.append(
                {
                    "persona_id": persona.persona_id,
                    "style": persona.style.value,
                    "truth": persona.persona_id,
                    "pre_rank": pre_rank,
                    "post_rank": post_rank,
                    "pre_words": sum(p.word_count for p in pre),
                    "post_words": sum(p.word_count for p in post),
                }
            )

        results[model] = {
            "drifted": _paired_summary(
                rows, "truth", bootstrap_seed=_seed_for(f"{seed}:{model}:drift")
            ),
            "stable_placebo": _paired_summary(
                controls, "truth", bootstrap_seed=_seed_for(f"{seed}:{model}:stable")
            ),
            "mean_pre_words": round(
                sum(row["pre_words"] for row in rows) / len(rows), 1
            )
            if rows
            else 0.0,
            "mean_post_words": round(
                sum(row["post_words"] for row in rows) / len(rows), 1
            )
            if rows
            else 0.0,
        }

    return {
        "design": {
            "kind": "paired within-persona",
            "seed": seed,
            "posts_per_persona": posts_per_persona,
            "drift_at": drift_at,
            "profile_posts": profile_posts,
            "query_posts": query_posts,
            "candidate_pool": len(pool),
            "drifted_personas": len(drifters),
            "stable_personas": len(stable),
            "note": (
                "Synthetic persona retrieval, not human authorship. The profile is "
                "fixed between conditions; only the query window moves across the "
                "recorded drift point. Template reusers are a placebo: they have no "
                "drift, so any pre/post change there is corpus noise."
            ),
        },
        "models": results,
    }


# --------------------------------------------------------------------------- #
# Sweeps
# --------------------------------------------------------------------------- #


def _run_facts(seed: int, n_clusters: int, services_per_cluster: int) -> dict[str, Any]:
    """Run the full harness once and keep the numbers a sweep compares."""
    cfg = FarmConfig(
        seed=seed,
        n_clusters=n_clusters,
        services_per_cluster=services_per_cluster,
    )
    result = harness_run(cfg)
    modules = result["modules"]
    correlation_module = modules["M2_correlation"]
    naive = correlation_module["naive"]["link_scoring"]
    frozen = correlation_module["at_frozen_threshold"]["link_scoring"]
    filtered = correlation_module["frequency_filtered"]["link_scoring"]
    held = result["held_out"]["M2_correlation"]
    rules = modules["M2_misconfig_rules"]
    entity = modules["M3_entity_resolution"]
    attribution_models = modules["M5_attribution"]["models"]

    return {
        "seed": seed,
        "n_clusters": n_clusters,
        "services_per_cluster": services_per_cluster,
        "farm_fingerprint": result["meta"]["farm_fingerprint"],
        "careful_recall": rules["careful_rules"]["recall"]["value"],
        "careful_precision": rules["careful_rules"]["precision"]["value"],
        "careful_fp": rules["careful_rules"]["fp"],
        "naive_rule_precision": rules["naive_rules"]["precision"]["value"],
        "naive_rule_fp": rules["naive_rules"]["fp"],
        "naive_link_precision": naive["precision"],
        "naive_false_merge": naive["false_merge_rate"],
        "naive_links": naive["proposed"],
        "frozen_link_precision": frozen["precision"],
        "frozen_false_merge": frozen["false_merge_rate"],
        "frozen_links": frozen["proposed"],
        "filtered_link_precision": filtered["precision"],
        "filtered_false_merge": filtered["false_merge_rate"],
        "filtered_links": filtered["proposed"],
        "naive_precision@1": correlation_module["naive"]["ranking"]["precision@1"][
            "value"
        ],
        "filtered_precision@1": correlation_module["frequency_filtered"]["ranking"][
            "precision@1"
        ]["value"],
        "heldout_naive_link_precision": held["naive"]["link_scoring"]["precision"],
        "heldout_filtered_link_precision": held["frequency_filtered"]["link_scoring"][
            "precision"
        ],
        "heldout_naive_false_merge": held["naive"]["link_scoring"]["false_merge_rate"],
        "heldout_filtered_false_merge": held["frequency_filtered"]["link_scoring"][
            "false_merge_rate"
        ],
        "entity_exact_precision": entity["exact_match"]["links"]["precision"],
        "entity_blocklisted_precision": entity["placeholder_blocklisted"]["links"][
            "precision"
        ],
        "attribution_text_full_drift_hit@3": attribution_models["text_full"][
            "drift_breakdown"
        ]["drifted_corpus_queries"]["hit@3"]["value"],
        "attribution_text_function_drift_hit@3": attribution_models["text_function"][
            "drift_breakdown"
        ]["drifted_corpus_queries"]["hit@3"]["value"],
        "frequency_cap": correlation_module["frequency_cap"],
        "correlation_threshold": result["thresholds"][0]["value"],
    }


def _aggregate(rows: Iterable[dict[str, Any]], keys: Sequence[str]) -> dict[str, Any]:
    rows = list(rows)
    return {key: _describe([row[key] for row in rows]) for key in keys}


_NUMERIC_KEYS: tuple[str, ...] = (
    "careful_recall",
    "careful_precision",
    "careful_fp",
    "naive_rule_precision",
    "naive_rule_fp",
    "naive_link_precision",
    "naive_false_merge",
    "naive_links",
    "frozen_link_precision",
    "frozen_false_merge",
    "frozen_links",
    "filtered_link_precision",
    "filtered_false_merge",
    "filtered_links",
    "naive_precision@1",
    "filtered_precision@1",
    "heldout_naive_link_precision",
    "heldout_filtered_link_precision",
    "heldout_naive_false_merge",
    "heldout_filtered_false_merge",
    "entity_exact_precision",
    "entity_blocklisted_precision",
    "attribution_text_full_drift_hit@3",
    "attribution_text_function_drift_hit@3",
    "frequency_cap",
    "correlation_threshold",
)


def seed_sweep(seeds: Sequence[int] = SWEEP_SEEDS) -> dict[str, Any]:
    """Run the default farm over many seeds and report the distribution."""
    rows = [_run_facts(seed, 4, 3) for seed in seeds]
    invariants = {
        "filtered_precision_all_one": all(
            row["filtered_link_precision"] == 1.0 for row in rows
        ),
        "filtered_false_merge_all_zero": all(
            row["filtered_false_merge"] == 0.0 for row in rows
        ),
        "careful_recall_all_one": all(row["careful_recall"] == 1.0 for row in rows),
        "careful_fp_all_zero": all(row["careful_fp"] == 0 for row in rows),
        "frozen_precision_below_one": sum(
            1 for row in rows if row["frozen_link_precision"] < 1.0
        ),
        "naive_precision_below_one": sum(
            1 for row in rows if row["naive_link_precision"] < 1.0
        ),
    }
    return {
        "design": {
            "seeds": list(seeds),
            "n_clusters": 4,
            "services_per_cluster": 3,
            "note": (
                "Seeds vary identifiers and corpora, not the planted collision graph: "
                "every seed plants the same classes of widespread and coincidental "
                "defaults. The correlation counts are therefore expected to be "
                "identical across seeds, and a zero SD here means the result is "
                "structural rather than a lucky draw. The topology sweep is the "
                "experiment that varies the structure."
            ),
        },
        "summary": _aggregate(rows, _NUMERIC_KEYS),
        "invariants": invariants,
        "runs": rows,
    }


def topology_sweep(
    topologies: Sequence[tuple[int, int]] = TOPOLOGIES,
    *,
    seeds: Sequence[int] = (20260925, 20260926, 20260927),
) -> dict[str, Any]:
    """Run the harness over farm shapes, three seeds each.

    The frequency cap is derived from ``services_per_cluster``, so a topology where
    the derivation is wrong should show up here as a cap that no longer separates
    true cluster artefacts from widespread defaults.
    """
    rows = [
        _run_facts(seed, clusters, services)
        for clusters, services in topologies
        for seed in seeds
    ]
    by_topology: dict[str, Any] = {}
    for clusters, services in topologies:
        group = [
            row
            for row in rows
            if row["n_clusters"] == clusters
            and row["services_per_cluster"] == services
        ]
        by_topology[f"{clusters}x{services}"] = {
            "summary": _aggregate(group, _NUMERIC_KEYS),
            "invariants": {
                "filtered_precision_all_one": all(
                    row["filtered_link_precision"] == 1.0 for row in group
                ),
                "filtered_false_merge_all_zero": all(
                    row["filtered_false_merge"] == 0.0 for row in group
                ),
            },
        }
    return {
        "design": {
            "topologies": [
                {"n_clusters": c, "services_per_cluster": s} for c, s in topologies
            ],
            "seeds": list(seeds),
            "note": (
                "services_per_cluster also sets the frequency cap, so this is a test "
                "of the derivation, not only of scale."
            ),
        },
        "by_topology": by_topology,
        "runs": rows,
    }


# --------------------------------------------------------------------------- #
# Cap ablation and paired significance
# --------------------------------------------------------------------------- #


def cap_ablation(seed: int = DEFAULT_SEED) -> dict[str, Any]:
    """Score the correlator at a range of fixed caps, including the derived one.

    A cap tuned to the evaluation set would show a peak at one value and a fall on
    either side. A cap derived from cluster size should be stable on a plateau, and
    values below the true cluster size should lose recall because they discard true
    cluster artefacts.
    """
    farm = generate_farm(FarmConfig(seed=seed))
    caps: list[int | None] = [None, 2, 3, 4, 5, 6, 8, 10]
    rows = []
    for cap in caps:
        scored = correlation_experiment(farm, max_artifact_frequency=cap)
        rows.append(
            {
                "cap": cap,
                "derived_cap": cap == farm.config.services_per_cluster,
                "proposed": scored["link_scoring"]["proposed"],
                "precision": scored["link_scoring"]["precision"],
                "recall": scored["link_scoring"]["recall"],
                "false_merge_rate": scored["link_scoring"]["false_merge_rate"],
                "precision@1": scored["ranking"]["precision@1"]["value"],
                "mrr": scored["ranking"]["mrr"],
            }
        )
    return {
        "design": {
            "seed": seed,
            "services_per_cluster": farm.config.services_per_cluster,
            "derived_cap": farm.config.services_per_cluster,
            "note": (
                "None is the naive correlator. The derived cap is max(2, "
                "services_per_cluster); it is not selected by looking at these rows."
            ),
        },
        "rows": rows,
    }


def _per_query_correlation(farm: Any, cap: int | None) -> list[dict[str, Any]]:
    rows = []
    for service in farm.services:
        if not service.cluster_id:
            continue
        mates = set(farm.cluster_mates(service.service_id))
        ranked = correlation.rank_services(
            farm,
            service.service_id,
            farm.correlation_pool(service.service_id),
            max_artifact_frequency=cap,
        )
        false_candidates = [
            candidate
            for candidate, score in ranked
            if score > 0 and candidate not in mates
        ]
        rows.append(
            {
                "service_id": service.service_id,
                "cluster_id": service.cluster_id,
                "top1_correct": bool(ranked) and ranked[0][0] in mates,
                "top1_score": round(ranked[0][1], 6) if ranked else 0.0,
                "nonmate_candidates": false_candidates,
            }
        )
    return rows


def _sign_test(greater: int, less: int) -> float:
    """Two-sided exact sign test for paired counts."""
    n = greater + less
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(greater, less) + 1))
    return min(1.0, 2.0 * tail / (2**n))


def correlation_paired_test(seed: int = DEFAULT_SEED) -> dict[str, Any]:
    """Compare naive and filtered correlation on the same queries.

    Aggregates hide which query produced a false link. This keeps the query as the
    unit, so the test asks whether the cap changed the same operator's result rather
    than whether two totals differ.
    """
    farm = generate_farm(FarmConfig(seed=seed))
    cap = farm.config.services_per_cluster
    naive = _per_query_correlation(farm, None)
    filtered = _per_query_correlation(farm, cap)

    top1_pre_only = sum(
        1 for a, b in zip(naive, filtered) if a["top1_correct"] and not b["top1_correct"]
    )
    top1_post_only = sum(
        1 for a, b in zip(naive, filtered) if b["top1_correct"] and not a["top1_correct"]
    )
    false_before = [len(row["nonmate_candidates"]) for row in naive]
    false_after = [len(row["nonmate_candidates"]) for row in filtered]
    more_after = sum(1 for a, b in zip(false_before, false_after) if b > a)
    more_before = sum(1 for a, b in zip(false_before, false_after) if a > b)
    naive_pairs = {
        tuple(sorted((row["service_id"], candidate)))
        for row in naive
        for candidate in row["nonmate_candidates"]
    }
    filtered_pairs = {
        tuple(sorted((row["service_id"], candidate)))
        for row in filtered
        for candidate in row["nonmate_candidates"]
    }

    return {
        "design": {
            "seed": seed,
            "cap": cap,
            "queries": len(naive),
            "note": (
                "The query is the unit. Top-1 correctness is matched per service. "
                "Non-mate candidates are counted per query (directed: the wrong names "
                "one query would surface) and again as distinct unordered pairs, which "
                "is the unit `score_links` uses."
            ),
        },
        "top1": {
            "naive_correct": sum(1 for row in naive if row["top1_correct"]),
            "filtered_correct": sum(1 for row in filtered if row["top1_correct"]),
            "naive_only": top1_pre_only,
            "filtered_only": top1_post_only,
            "mcnemar_p_exact": round(mcnemar_exact(top1_pre_only, top1_post_only), 6),
        },
        "false_links": {
            "naive_nonmate_candidates_total": sum(false_before),
            "filtered_nonmate_candidates_total": sum(false_after),
            "naive_unique_false_pairs": len(naive_pairs),
            "filtered_unique_false_pairs": len(filtered_pairs),
            "queries_with_more_after": more_after,
            "queries_with_more_before": more_before,
            "sign_test_p_exact": round(_sign_test(more_after, more_before), 6),
            "delta_mean_candidates_per_query": round(
                sum(b - a for a, b in zip(false_before, false_after)) / len(naive), 6
            ),
            "delta_ci95": paired_bootstrap(
                false_before,
                false_after,
                seed=_seed_for(f"{seed}:false-links"),
            )["ci95"],
        },
        "per_query": [
            {
                "service_id": n["service_id"],
                "cluster_id": n["cluster_id"],
                "naive_top1": n["top1_correct"],
                "filtered_top1": f["top1_correct"],
                "naive_nonmate_candidates": len(n["nonmate_candidates"]),
                "filtered_nonmate_candidates": len(f["nonmate_candidates"]),
            }
            for n, f in zip(naive, filtered)
        ],
    }


# --------------------------------------------------------------------------- #
# The whole suite
# --------------------------------------------------------------------------- #


def run_all() -> dict[str, Any]:
    """Run every experiment and return one JSON-serialisable document."""
    return {
        "meta": {
            "suite": "eval.experiments",
            "default_seed": DEFAULT_SEED,
            "sweep_seeds": list(SWEEP_SEEDS),
            "note": (
                "All experiments are deterministic. The paired drift corpora are "
                "regenerated from string seeds; the bootstrap seeds are recorded."
            ),
        },
        "paired_drift": paired_drift(),
        "seed_sweep": seed_sweep(),
        "topology_sweep": topology_sweep(),
        "cap_ablation": cap_ablation(),
        "correlation_paired_test": correlation_paired_test(),
    }
