"""Authorship attribution, baseline (M5).

Implements the Part E3 ablation design as four separate, separately-reported models,
because the plan's claim -- that behavioural features may beat lexical features on
short posts -- is a *hypothesis to test*, not a premise to assume:

  * `text_full`     -- relative frequency over all tokens. Sensitive to content, and
                       therefore to the style drift the farm applies.
  * `text_function` -- restricted to function words. The classical stylometric signal,
                       and the one that should survive a rebrand.
  * `behaviour`     -- posting-hour histogram, cadence, template reuse, length mix.
  * `combined`      -- function words plus behaviour.

**Scope limit, stated here because it must be stated in the report:** these personas are
generated with known parameters, so a model that recovers them has recovered the
generator, not a property of human writing. Every number computed here is *synthetic
persona retrieval*. It is not validated authorship attribution, and reporting it as
such would be the easiest way to lose credibility in a viva.

Query construction avoids leakage: each persona's posts are split by time into a
profile portion (earlier posts) and a held-out query portion (later posts). The query
is never part of the profile it is scored against, so a persona cannot be matched to
itself by memorisation.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Sequence

from farm.language import FUNCTION_WORD_SETS

_TOKEN = re.compile(r"[a-z']+")

# The full function-word vocabulary, used to build the function-word feature space.
FUNCTION_WORD_VOCABULARY: frozenset[str] = frozenset(
    word for words in FUNCTION_WORD_SETS.values() for word in words
)


def tokenize(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def cosine(left: dict[str, float], right: dict[str, float]) -> float:
    """Cosine similarity between two sparse feature maps."""
    if not left or not right:
        return 0.0
    keys = left.keys() & right.keys()
    if not keys:
        return 0.0
    dot = sum(left[k] * right[k] for k in keys)
    left_norm = math.sqrt(sum(v * v for v in left.values()))
    right_norm = math.sqrt(sum(v * v for v in right.values()))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


# --------------------------------------------------------------------------- #
# Feature extraction
# --------------------------------------------------------------------------- #


def text_profile(posts: Sequence, *, function_words_only: bool = False) -> dict[str, float]:
    """Relative token frequencies, optionally restricted to function words."""
    counter: Counter = Counter()
    for post in posts:
        for token in tokenize(post.text):
            if function_words_only and token not in FUNCTION_WORD_VOCABULARY:
                continue
            counter[token] += 1
    total = sum(counter.values())
    if not total:
        return {}
    return {token: count / total for token, count in counter.items()}


def behaviour_profile(posts: Sequence) -> dict[str, float]:
    """Behavioural features: when, how often, how long, and whether templated.

    Deliberately uses no lexical content at all, so the ablation stays clean and the
    comparison against the text models means something.
    """
    if not posts:
        return {}

    hours: Counter = Counter(p.hour_of_day for p in posts)
    hour_total = sum(hours.values())
    features = {f"hour_{h:02d}": hours.get(h, 0) / hour_total for h in range(24)}

    ordered = sorted(p.day_index for p in posts)
    gaps = [b - a for a, b in zip(ordered, ordered[1:])] or [0]
    features["mean_gap"] = min(1.0, (sum(gaps) / len(gaps)) / 7.0)
    features["template_ratio"] = sum(1 for p in posts if p.is_template) / len(posts)
    features["mean_length"] = min(
        1.0, (sum(p.word_count for p in posts) / len(posts)) / 300.0
    )
    features["short_ratio"] = sum(1 for p in posts if p.length_bucket == "short") / len(
        posts
    )
    return features


# --------------------------------------------------------------------------- #
# Query construction
# --------------------------------------------------------------------------- #

MODELS: tuple[str, ...] = ("text_full", "text_function", "behaviour", "combined")


@dataclass(frozen=True)
class RetrievalQuery:
    """A held-out set of posts, and the persona who actually wrote them."""

    persona_id: str
    posts: tuple
    length_bucket: str
    # True when at least one held-out post was written *after* this persona's drift
    # point, read from the farm's recorded ground truth. Not inferred from length,
    # post count, or the text itself.
    drifted: bool


def build_queries(farm, *, profile_fraction: float = 0.6) -> list[RetrievalQuery]:
    """Split each persona's corpus by time into a profile part and a query part.

    Later posts form the query. That ordering matters because the farm applies style
    drift partway through a corpus, so a query drawn from later posts comes from
    *after* the rebrand. A model that learned only the earlier vocabulary is tested on
    exactly the material it should fail on.

    Splitting by time rather than at random is the same principle as the by-cluster
    split: a random split lets near-duplicate material sit on both sides, and the
    resulting score measures memorisation rather than generalisation.
    """
    queries: list[RetrievalQuery] = []
    for persona in farm.personas:
        posts = sorted(persona.posts, key=lambda p: (p.day_index, p.post_id))
        if len(posts) < 3:
            continue
        cut = max(1, int(len(posts) * profile_fraction))
        held_out = posts[cut:]
        if not held_out:
            continue
        buckets = Counter(p.length_bucket for p in held_out)
        queries.append(
            RetrievalQuery(
                persona_id=persona.persona_id,
                posts=tuple(held_out),
                length_bucket=(
                    "long"
                    if buckets.get("long", 0) >= buckets.get("short", 0)
                    else "short"
                ),
                drifted=(
                    persona.drift_at is not None
                    and any(p.index >= persona.drift_at for p in held_out)
                ),
            )
        )
    return queries


def profile_posts(farm, persona_id: str, query: RetrievalQuery) -> list:
    """The profile half of a persona's corpus: everything not held out in the query."""
    persona = next(p for p in farm.personas if p.persona_id == persona_id)
    held_out = {p.post_id for p in query.posts}
    return [p for p in persona.posts if p.post_id not in held_out]


def features_for(posts: Sequence, model: str) -> dict[str, float]:
    """Build the feature map for one model, from one set of posts."""
    if model == "text_full":
        return text_profile(posts)
    if model == "text_function":
        return text_profile(posts, function_words_only=True)
    if model == "behaviour":
        return behaviour_profile(posts)
    if model == "combined":
        # Behaviour features are prefixed so the two spaces cannot collide when the
        # sparse maps are unioned for the cosine.
        merged = dict(text_profile(posts, function_words_only=True))
        merged.update(
            {f"bh_{k}": v for k, v in behaviour_profile(posts).items()}
        )
        return merged
    raise ValueError(f"unknown model {model!r}; expected one of {MODELS}")


def rank_personas(
    farm,
    query: RetrievalQuery,
    model: str,
    *,
    pool: Sequence[str] | None = None,
) -> list[tuple[str, float]]:
    """Rank candidate personas for one query under one model.

    Candidates with zero similarity are omitted rather than returned at score 0, so
    "no candidate" stays distinguishable from "a weak candidate". That distinction is
    what allows the abstention cases to be scored at all.
    """
    candidates = list(pool) if pool is not None else [p.persona_id for p in farm.personas]
    query_features = features_for(query.posts, model)

    ranked: list[tuple[str, float]] = []
    for persona_id in candidates:
        # The query author is a legitimate candidate. Its profile excludes the held-out
        # posts, so this is a genuine generalisation test rather than self-comparison.
        profile = features_for(profile_posts(farm, persona_id, query), model)
        score = cosine(query_features, profile)
        if score > 0:
            ranked.append((persona_id, score))

    ranked.sort(key=lambda item: (-item[1], item[0]))
    return ranked


# --------------------------------------------------------------------------- #
# Evaluation
# --------------------------------------------------------------------------- #


def _bucket_metrics(
    farm,
    rankings: list[list[str]],
    relevant: list[set[str]],
) -> dict:
    """Metrics for one subset of queries, with the random baseline alongside."""
    from eval.metrics import hit_at_k, mean_reciprocal_rank, recall_at_k

    pool = len(farm.personas)
    return {
        "n_queries": len(rankings),
        "pool_size": pool,
        "hit@1": hit_at_k(rankings, relevant, 1).to_dict(),
        "hit@3": hit_at_k(rankings, relevant, 3).to_dict(),
        "recall@5": round(recall_at_k(rankings, relevant, 5), 6),
        "mrr": round(mean_reciprocal_rank(rankings, relevant), 6),
        "random_baseline": {
            "hit@1": round(min(1.0, 1 / pool), 6) if pool else 0.0,
            "hit@3": round(min(1.0, 3 / pool), 6) if pool else 0.0,
            "recall@5": round(min(1.0, 5 / pool), 6) if pool else 0.0,
        },
    }


def evaluate(farm, models: Sequence[str] = MODELS) -> dict:
    """Score every model, split by post length, with the random baseline attached.

    Length stratification is the Part E3 requirement: a flat R@5 over mixed lengths
    hides the fact that short posts carry far less authorship signal, and reporting a
    single number invites the reader to assume the long-text figure applies everywhere.

    The `drift_breakdown` is the other half of the story. Queries are split by whether
    any held-out post was written after their persona's *recorded* drift point, which is
    what separates a model that learned vocabulary from one that learned style. The
    pre-drift bucket is the control: without it, a model that is simply weak everywhere
    would look like a model that is robust to drift.
    """
    queries = build_queries(farm)
    results: dict = {}

    for model in models:
        by_bucket: dict[str, tuple[list, list]] = {
            "long": ([], []),
            "short": ([], []),
        }
        drifted: tuple[list, list] = ([], [])
        stable: tuple[list, list] = ([], [])

        for query in queries:
            ranked = [persona_id for persona_id, _ in rank_personas(farm, query, model)]
            truth = {query.persona_id}

            by_bucket[query.length_bucket][0].append(ranked)
            by_bucket[query.length_bucket][1].append(truth)
            (drifted if query.drifted else stable)[0].append(ranked)
            (drifted if query.drifted else stable)[1].append(truth)

        entry = {
            "features": (
                {"text_full": "all tokens", "text_function": "function words only",
                 "behaviour": "hours, cadence, templates, length",
                 "combined": "function words + behaviour"}[model]
            ),
            "long": _bucket_metrics(farm, *by_bucket["long"]),
            "short": _bucket_metrics(farm, *by_bucket["short"]),
            "drift_breakdown": {
                "drifted_corpus_queries": _bucket_metrics(farm, *drifted),
                "stable_corpus_queries": _bucket_metrics(farm, *stable),
            },
        }
        results[model] = entry

    return {
        "scope_limit": (
            "synthetic persona retrieval -- NOT validated human authorship attribution. "
            "Personas are generated with known parameters, so recovering them recovers "
            "the generator."
        ),
        "models": results,
    }


def evaluate_abstention(
    farm,
    model: str = "text_function",
    *,
    min_score: float | None = None,
) -> dict:
    """Score the retrieval abstention cases: queries whose author is not in the pool.

    A correct system returns nothing. Returning a top-1 is a false association, and that
    is the failure mode this measures.

    `min_score` applies the *frozen* threshold, which is the point of fitting one. With
    `min_score=None` the baseline asserts whenever any candidate has non-zero
    similarity, which on a small pool is almost always -- so the difference between the
    two runs is itself the finding, and both are reported rather than only the
    flattering one.
    """
    from eval.metrics import score_abstention

    queries = {q.persona_id: q for q in build_queries(farm)}
    asserted: list[bool] = []
    details: list[dict] = []

    for case in farm.abstention_cases:
        if case.kind != "retrieval":
            continue
        owner = None
        for persona in farm.personas:
            if any(p.post_id == case.query for p in persona.posts):
                owner = persona.persona_id
                break
        if owner is None or owner not in queries:
            continue

        ranked = rank_personas(farm, queries[owner], model, pool=case.pool)
        top_score = ranked[0][1] if ranked else 0.0
        returned = bool(ranked) and (min_score is None or top_score >= min_score)
        asserted.append(returned)
        details.append(
            {
                "case_id": case.case_id,
                "returned": returned,
                "top": ranked[0][0] if ranked else None,
                "top_score": round(top_score, 6),
                "threshold_applied": (
                    None if min_score is None else round(min_score, 6)
                ),
                "why_absent": case.why_absent,
            }
        )

    scoring = score_abstention(asserted)
    return {
        "model": model,
        "thresholded": min_score is not None,
        "min_score": None if min_score is None else round(min_score, 6),
        "scoring": scoring.to_dict(),
        "cases": details,
    }