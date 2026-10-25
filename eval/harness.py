"""The evaluation harness.

Orchestrates a full run: build the farm, split it, freeze thresholds on dev, score every
module on test, and write results that carry their own reproduction command.

The ordering is not incidental. Part E2 requires thresholds to be fixed **before** the
test numbers are seen, so the harness does exactly that in that order and records the
threshold decision alongside the result. Nothing here may inspect a test score in order
to choose a parameter; if that were ever needed, it would mean the threshold belonged in
the dev fit.
"""

from __future__ import annotations

import hashlib
from typing import Any

from farm.generator import generate_farm
from farm.model import FarmConfig

from eval.detectors import attribution, correlation, entity, misconfig, provenance
from eval.metrics import score_links
from eval.splits import ThresholdDecision, choose_threshold, cluster_split

HARNESS_VERSION = "0.1.0"

#: How each scored module is described in the report. M1 is measured against the
#: fixture seam, not the network: the collectors exist, but live acquisition is dark.
M1_COLLECTION_STATUS = "fixture-verified"


def farm_fingerprint(farm) -> str:
    """Digest of the farm, so a result file can be tied to the exact data behind it."""
    return hashlib.sha256(farm.to_json().encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# Correlation: ranking metrics and link scoring
# --------------------------------------------------------------------------- #


def correlation_experiment(
    farm,
    *,
    max_artifact_frequency: int | None,
    score_threshold: float = 0.0,
) -> dict[str, Any]:
    """Score the correlator as both a ranker and a link proposer.

    Two views, because they answer different questions:

      * **ranking** -- per query service, sort the candidate pool and ask whether the
        true cluster-mate came first. This is Part E1's precision@1.
      * **link scoring** -- propose *every* pair above a score threshold and count them
        against the truth. This is Part E2's candidate-link precision, and it is where
        a correlator that links everything gets caught.

    A correlator can look respectable on ranking while proposing a large number of
    spurious links, so both numbers are reported.
    """
    from eval.metrics import hit_at_k, mean_reciprocal_rank, precision_at_1, recall_at_k

    rankings: list[list[str]] = []
    relevant: list[set[str]] = []
    proposed: list[tuple[str, str]] = []

    for service in farm.services:
        if not service.cluster_id:
            continue
        pool = farm.correlation_pool(service.service_id)
        ranked = correlation.rank_services(
            farm,
            service.service_id,
            pool,
            max_artifact_frequency=max_artifact_frequency,
        )
        rankings.append([candidate for candidate, _ in ranked])
        relevant.append(set(farm.cluster_mates(service.service_id)))
        for candidate, score in ranked:
            if score >= score_threshold:
                proposed.append((service.service_id, candidate))

    truth = {
        (a, b)
        for service in farm.services
        if service.cluster_id
        for b in farm.cluster_mates(service.service_id)
        for a in [service.service_id]
    }
    universe = [
        (a.service_id, b.service_id)
        for a in farm.services
        for b in farm.services
        if a.service_id != b.service_id
    ]

    p1 = precision_at_1(rankings, relevant)
    links = score_links(proposed, truth, universe=universe, threshold=score_threshold)

    return {
        "variant": (
            "frequency-filtered" if max_artifact_frequency is not None else "naive"
        ),
        "max_artifact_frequency": max_artifact_frequency,
        "ranking": {
            "n_queries": p1.total,
            "pool_size": len(farm.services) - 1,
            "precision@1": p1.to_dict(),
            "mrr": round(mean_reciprocal_rank(rankings, relevant), 6),
            "recall@3": round(recall_at_k(rankings, relevant, 3), 6),
            "hit@3": hit_at_k(rankings, relevant, 3).to_dict(),
            "random_baseline_precision@1": round(
                farm.precision_at_1_baseline(restricted_to_clustered=False), 6
            ),
            "random_baseline_precision@1_restricted": round(
                farm.precision_at_1_baseline(restricted_to_clustered=True), 6
            ),
            "baseline_note": (
                "the baseline describes the pool actually ranked: "
                f"{len(farm.correlation_pool(next(s.service_id for s in farm.services if s.cluster_id)))} "
                "candidates, singletons included (2/13 = 0.1538). The restricted figure "
                "(2/11 = 0.1818) applies only if singletons are excluded from the pool, "
                "which is not what this run does -- reporting it here would put a number "
                "next to a result that describes a different experiment."
            ),
        },
        "link_scoring": links.to_dict(),
    }


def collection_integrity(farm) -> dict[str, Any]:
    """M1: exercise the acquisition seam the collector must satisfy, on fixtures.

    Runs the fixture path end to end -- gate, collector, checkpoints, WARC -- and reports
    seam properties rather than network bytes:

    * an out-of-scope host is refused before the request exists, unsent;
    * a full run stores findings, and the same scope run again stores nothing and
      issues no further requests;
    * every path that did work leaves a cursor where it stopped;
    * every stored snapshot points at its own WARC record, and no seed is added by
      the collector itself.

    Scored as ``fixture-verified`` rather than live-tested, and the report says so:
    the fixture transport is in-memory pages and the live fetcher is dark. A pass here
    means the machinery is gated and resumable; it does not mean live collection works.
    """
    from pathlib import Path
    from tempfile import TemporaryDirectory

    from collect.checkpoints import CheckpointStore
    from collect.clock import FrozenClock
    from collect.collector import (
        SEARCH_PROVIDER_AHMIA,
        CollectionPolicy,
        CollectionScope,
        Collector,
        OriginSummary,
    )
    from collect.errors import ScopeViolation
    from collect.fixtures import build_fixtures
    from collect.schedule import Budget, Politeness
    from gates.gate import CollectionGate
    from gates.roles import Principal, Role

    notes: list[str] = []
    try:
        clock = FrozenClock.at("2026-09-29T12:00:00+00:00")
        with TemporaryDirectory(prefix="m1-integrity-") as root:
            gate = CollectionGate(Path(root), clock=clock.as_gate_clock())
            owner = Principal(principal_id="m1-integrity", role=Role.CASE_OWNER)
            gate.acknowledge(owner)
            case_id = "CASE-M1"
            gate.open_case(
                owner,
                case_id=case_id,
                title="M1 collection-integrity probe",
                authorisation_ref="AUTH-M1",
                retention_days=30,
            )
            fixture_set = build_fixtures(
                farm,
                clock=clock,
                service_limit=3,
                channel_limit=1,
                page_size=3,
                ct_limit=3,
            )
            scope = CollectionScope(
                onion_addresses=fixture_set.onion_targets,
                # The M1 scorer measures the path it was written against, which is the
                # public index served by the fixture transport. The local index answers the
                # same queries offline and is the product default; scoring it here would be
                # scoring a different mechanism under the same name.
                search_provider=SEARCH_PROVIDER_AHMIA,
                search_queries=(fixture_set.seed_query,),
                ct_queries=fixture_set.ct_queries,
                telegram_channels=fixture_set.channels,
            )
            policy = CollectionPolicy(politeness=Politeness(min_host_delay_seconds=0.0))
            collector = Collector(
                gate=gate, transport=fixture_set.transport, clock=clock, policy=policy
            )

            try:
                collector._fetch(  # noqa: SLF001 - the seam is the unit under test
                    "http://not-declared-anywhere.onion/",
                    scope=scope,
                    case_id=case_id,
                    seed_label="intruder",
                    summary=OriginSummary(origin="onion_service"),
                    budget=Budget(policy.politeness),
                )
                refused_unsent = False
            except ScopeViolation:
                refused_unsent = len(fixture_set.transport.fetched) == 0
            notes.append(f"out-of-scope refused before the request: {refused_unsent}")

            first = collector.collect(case_id=case_id, principal=owner, scope=scope)
            fetched_after_first = len(fixture_set.transport.fetched)
            second = collector.collect(case_id=case_id, principal=owner, scope=scope)
            idempotent = (
                second.snapshots_stored == 0
                and len(fixture_set.transport.fetched) == fetched_after_first
            )
            notes.append(f"first run stored {first.snapshots_stored}; second stored 0, "
                         f"fetched 0 new: {idempotent}")

            store = CheckpointStore(gate.cases.derived_dir(case_id), clock=clock)
            snapshots = gate.collected_snapshots(owner, case_id)
            known_records = {record.record_id for record in collector.records}
            snapshot_records = [snapshot.get("warc_record_id") for snapshot in snapshots]
            warc_ok = (
                len(collector.records) == fetched_after_first
                and all(record_id in known_records for record_id in snapshot_records)
            )
            notes.append(f"cursors={len(store.sources())} snapshots={len(snapshots)} "
                         f"warc_records={len(collector.records)} linked={warc_ok}")

            seeds_untouched = list(gate.cases.get(case_id).seeds) == []
            checks = {
                "scope_refused_unsent": bool(refused_unsent),
                "first_run_stored": int(first.snapshots_stored),
                "second_run_idempotent": bool(idempotent),
                "checkpoints_saved": len(store.sources()) > 0,
                "warc_records_linked": bool(warc_ok),
                "no_self_seeding": bool(seeds_untouched),
            }
            passed = (
                checks["scope_refused_unsent"]
                and checks["first_run_stored"] > 0
                and checks["second_run_idempotent"]
                and checks["checkpoints_saved"]
                and checks["warc_records_linked"]
                and checks["no_self_seeding"]
            )
            return {
                "status": M1_COLLECTION_STATUS,
                "passed": bool(passed),
                "checks": checks,
                "notes": notes,
            }
    except Exception as error:  # noqa: BLE001 - the report shows the failure, not a traceback
        notes.append(f"probe failed: {error}")
        return {
            "status": M1_COLLECTION_STATUS,
            "passed": False,
            "checks": {"probe_ran": False},
            "notes": notes,
        }


def correlation_abstention(
    farm,
    *,
    max_artifact_frequency: int | None,
    min_score: float | None = None,
) -> dict[str, Any]:
    """Score the correlation abstention cases: singleton services with no true mate.

    A singleton has no cluster-sibling, so *any* proposed link is a false merge. This is
    where a correlator that links on weak evidence is caught, and it is why the farm
    carries singletons at all.

    With `min_score=None` a link is proposed whenever any artefact key agrees, which is
    the unthresholded baseline. Passing the frozen threshold shows what the system would
    actually do, and the pair of numbers is more informative than either alone.
    """
    from eval.metrics import score_abstention

    asserted: list[bool] = []
    details: list[dict[str, Any]] = []

    for case in farm.abstention_cases:
        if case.kind != "correlation":
            continue
        ranked = correlation.rank_services(
            farm,
            case.query,
            case.pool,
            max_artifact_frequency=max_artifact_frequency,
        )
        top_score = ranked[0][1] if ranked else 0.0
        returned = bool(ranked) and (min_score is None or top_score >= min_score)
        asserted.append(returned)
        details.append(
            {
                "case_id": case.case_id,
                "returned": returned,
                "top": ranked[0][0] if ranked else None,
                "top_score": round(top_score, 6),
            }
        )

    return {
        "variant": (
            "frequency-filtered" if max_artifact_frequency is not None else "naive"
        ),
        "thresholded": min_score is not None,
        "min_score": None if min_score is None else round(min_score, 6),
        "scoring": score_abstention(asserted).to_dict(),
        "cases": details,
    }


# --------------------------------------------------------------------------- #
# Threshold fitting (dev only)
# --------------------------------------------------------------------------- #


def fit_correlation_threshold(
    farm,
    split,
    *,
    min_precision: float = 0.95,
) -> ThresholdDecision:
    """Fit the link-score threshold on dev clusters only.

    Candidate scores carry an `is_true` label from the cluster ground truth, so this
    reduces to: pick the lowest score cut that holds precision at or above the floor.
    The lowest cut gives the highest recall subject to that constraint, which is the
    precision-dominant policy Part E2 specifies.
    """
    points: list[tuple[float, bool]] = []
    for service in farm.services:
        if not service.cluster_id or not split.is_dev(service.cluster_id):
            continue
        mates = set(farm.cluster_mates(service.service_id))
        for candidate, score in correlation.rank_services(
            farm, service.service_id, farm.correlation_pool(service.service_id)
        ):
            points.append((score, candidate in mates))

    return choose_threshold(
        "correlation_link_score", points, min_precision=min_precision
    )


def fit_retrieval_threshold(
    farm,
    split,
    *,
    model: str = "text_function",
    min_precision: float = 0.95,
) -> ThresholdDecision:
    """Fit the attribution score threshold on dev personas only.

    For every dev query, the author's own score is a positive and every other candidate's
    score is a negative. The resulting threshold is what would let the system decline to
    answer when nothing scores highly enough -- which is precisely the behaviour the
    retrieval abstention cases measure.
    """
    queries = attribution.build_queries(farm)
    persona_cluster = {p.persona_id: p.cluster_id for p in farm.personas}

    points: list[tuple[float, bool]] = []
    for query in queries:
        if not split.is_dev(persona_cluster.get(query.persona_id)):
            continue
        for candidate, score in attribution.rank_personas(farm, query, model):
            points.append((score, candidate == query.persona_id))

    return choose_threshold("attribution_score", points, min_precision=min_precision)


# --------------------------------------------------------------------------- #
# Full run
# --------------------------------------------------------------------------- #


def run(
    cfg: FarmConfig | None = None,
    *,
    dev_fraction: float = 0.5,
    min_precision: float = 0.95,
) -> dict[str, Any]:
    """Run the full evaluation and return a results document.

    Order of operations is the contract: build, split, fit on dev, then score. No
    parameter is adjusted after a test score has been seen.
    """
    from farm.services import rules_with_hard_negative_fixture

    cfg = cfg or FarmConfig()
    farm = generate_farm(cfg)
    split = cluster_split(farm, dev_fraction)

    # --- 1. fit thresholds on dev, before any test score exists ------------------- #
    correlation_threshold = fit_correlation_threshold(
        farm, split, min_precision=min_precision
    )
    retrieval_threshold = fit_retrieval_threshold(
        farm, split, min_precision=min_precision
    )

    # --- 2. score every module --------------------------------------------------- #
    frequency_cap = correlation.common_value_threshold(farm, cfg.services_per_cluster)

    careful = misconfig.evaluate(farm, misconfig.RULES)
    naive_rules = misconfig.evaluate(farm, misconfig.NAIVE_RULES)

    modules: dict[str, Any] = {
        "M1_collection": collection_integrity(farm),
        "M2_misconfig_rules": {
            "careful_rules": careful.to_dict(farm),
            "naive_rules": naive_rules.to_dict(farm),
            "rules_with_hard_negative_fixture": rules_with_hard_negative_fixture(),
            "separation_note": (
                "Both tables are scored against the same ground truth. The gap between "
                "them is what demonstrates the negative fixtures can fail a careless "
                "rule set, and therefore that the careful table's score means something."
            ),
        },
        "M2_correlation": {
            "naive": correlation_experiment(farm, max_artifact_frequency=None),
            "frequency_filtered": correlation_experiment(
                farm, max_artifact_frequency=frequency_cap
            ),
            "frequency_cap": frequency_cap,
            "frozen_link_threshold": correlation_threshold.to_dict(),
            "at_frozen_threshold": correlation_experiment(
                farm,
                max_artifact_frequency=None,
                score_threshold=correlation_threshold.value,
            ),
        },
        "M3_entity_resolution": {
            "exact_match": entity.evaluate(farm, block_placeholder=False),
            "placeholder_blocklisted": entity.evaluate(farm, block_placeholder=True),
        },
        "M5_attribution": attribution.evaluate(farm),
    }
    modules["M5_attribution"]["frozen_score_threshold"] = retrieval_threshold.to_dict()

    ledger = provenance.build_ledger(farm)
    modules["E4_provenance"] = {
        "ledger_entry_count": ledger["entry_count"],
        "manifest_sha256": ledger["manifest_sha256"],
        "verification": provenance.verify_ledger(farm, ledger),
        "tamper_self_test": provenance.tamper_detection_self_test(farm, ledger),
        "assumptions": ledger["assumptions"],
    }

    # --- 3. abstention ----------------------------------------------------------- #
    abstention = {
        "correlation_unthresholded": correlation_abstention(
            farm, max_artifact_frequency=None
        ),
        "correlation_filtered": correlation_abstention(
            farm, max_artifact_frequency=frequency_cap
        ),
        "correlation_at_frozen_threshold": correlation_abstention(
            farm,
            max_artifact_frequency=None,
            min_score=correlation_threshold.value,
        ),
        "retrieval_unthresholded": attribution.evaluate_abstention(farm),
        "retrieval_at_frozen_threshold": attribution.evaluate_abstention(
            farm, min_score=retrieval_threshold.value
        ),
        "note": (
            "A false assertion is the system answering a question it cannot answer. For "
            "a tool that proposes candidate identities this is the failure that produces "
            "a wrong name, so it is reported separately from recall rather than averaged "
            "into it. Thresholded and unthresholded runs are both reported: the "
            "difference is the finding, and publishing only the better number would hide "
            "that a threshold is doing the work."
        ),
    }

    return {
        "meta": {
            "harness_version": HARNESS_VERSION,
            "seed": cfg.seed,
            "farm_fingerprint": farm_fingerprint(farm),
            "farm_config": cfg.to_dict(),
            "reproduction_command": f"python -m eval run --seed {cfg.seed} --out ./out",
            "precision_floor": min_precision,
        },
        "split": split.to_dict(),
        "thresholds": [
            correlation_threshold.to_dict(),
            retrieval_threshold.to_dict(),
        ],
        "modules": modules,
        "abstention": abstention,
        "limitations": _limitations(farm, split),
    }


def _limitations(farm, split) -> list[str]:
    """Limitations that must travel with the results, not live only in the README."""
    from farm.catalogue import SINGLE_FLAWS
    from farm.services import rules_with_hard_negative_fixture

    clustered = [s for s in farm.services if s.cluster_id]
    gaps = sorted(
        {s.kind for s in SINGLE_FLAWS} - set(rules_with_hard_negative_fixture())
    )

    return [
        "Attribution results are SYNTHETIC PERSONA RETRIEVAL, not validated human "
        "authorship attribution. Personas are generated with known parameters, so "
        "recovering them recovers the generator.",
        f"Sample size is small: {len(clustered)} clustered services across "
        f"{len(farm.clusters)} clusters, {len(farm.personas)} personas. Confidence "
        "intervals are reported because the point estimates alone would overstate what "
        "the sample supports.",
        f"The test split holds out {len(split.test_clusters)} of {len(farm.clusters)} "
        "clusters, so evaluation is on unseen operators by construction, but the test "
        "set is small and the interval correspondingly wide.",
        "Frequency filtering addresses widespread vendor-default artefacts and not "
        "coincidental ones. The coincidental class needs a maintained blocklist of known "
        "defaults, which this implementation does not have.",
        f"Rules without an implemented negative fixture: {gaps}. Their false-positive "
        "rates are measured only against true positives and are therefore optimistic; "
        "this is reported rather than left implied.",
        "Provenance establishes byte consistency against a recorded digest. It is not "
        "chain of custody and does not establish admissibility.",
    ]