"""Farm orchestration: assemble the whole synthetic farm from a seed.

`generate_farm(FarmConfig(seed=...))` is the single entry point. Given the same
config, it produces a byte-identical FarmTruth, which is what lets the report cite a
seed instead of shipping fixtures (Part E: "numbers without a reproduction command
are opinions").

Assembly order matters and is deliberate:
  1. services and clusters
  2. SINGLE-scope flaws        -> rule recall / FP ground truth
  3. CLUSTER-scope artefacts   -> genuine links, the correlator's positive class
  4. stock-default artefacts   -> spurious links, the correlator's negative class
  5. personas                  -> subjects, then decoys that *look like* subjects
  6. corpora                   -> with style drift applied to some personas
  7. abstention cases          -> the queries whose answer is "I do not know"
"""

from __future__ import annotations

import random

from farm.catalogue import CLUSTER_FLAWS, SINGLE_FLAWS, get_spec
from farm.language import LISTING_TEMPLATES, TOPIC_VOCABULARY
from farm.model import (
    AbstentionCase,
    Cluster,
    FarmConfig,
    FarmTruth,
    PersonaRole,
    Service,
    StyleRegime,
    deterministic_hex,
)
from farm.personas import build_persona, generate_posts, make_handle
from farm.services import (
    CLUSTER_FLAW_ARTIFACT_KEY,
    FIXTURE_BUILDERS,
    assign_cluster_artifact,
    assign_stock_default,
    inject_single_flaw,
    make_flaw_record,
    make_service,
    unbuildable_flaw_kinds,
)

_TOPICS: tuple[str, ...] = tuple(TOPIC_VOCABULARY)

# Cluster-flaw kinds are dealt round-robin over the clusters, so no two clusters
# share a *kind* with the same value. Two clusters sharing a kind is fine and
# realistic (both operators enabled mod_status); sharing a *value* is not, because
# that would create an unlabelled false link in the ground truth.
#: One entry per persona slot in a cluster, so the mix is deterministic and each regime the farm
#: declares is actually built. Its length must equal `FarmConfig.personas_per_cluster`: with a
#: longer cycle the trailing regimes fall past the last slot and are silently never generated. `HINGLISH_HEAVY` was in `StyleRegime` and absent from this tuple,
#: which meant the one regime the code-mix block exists for was never generated: the feature was
#: scored on light mixing only, at n=4, while the report described it as the differentiator.
#: A regime that is declared and never instantiated is a design gap, not a smaller sample.
_STYLE_CYCLE: tuple[StyleRegime, ...] = (
    StyleRegime.PURE_ENGLISH,
    StyleRegime.HINGLISH_LIGHT,
    StyleRegime.HINGLISH_HEAVY,
    StyleRegime.TEMPLATE_REUSER,
)

_FUNCTION_SET_CYCLE: tuple[str, ...] = ("formal", "casual", "terse", "hedging")

# How many artefact kinds to plant as stock defaults, per class. See
# _inject_stock_defaults for why the two classes are separate: they need different
# defences, and a farm with only one of them cannot distinguish a careful correlator
# from a careless one.
N_WIDESPREAD_DEFAULT_PLANTS = 2
N_COINCIDENTAL_DEFAULT_PLANTS = 4


class FarmConfigError(ValueError):
    """Raised when a config cannot produce a well-formed farm."""


def validate_config(cfg: FarmConfig) -> None:
    """Fail loudly on configs that would make the farm's ground truth wrong."""
    if unbuildable_flaw_kinds():  # pragma: no cover - guarded by tests
        raise FarmConfigError(
            f"catalogue has flaw kinds with no fixture path: {unbuildable_flaw_kinds()}"
        )
    if cfg.n_clusters < 1:
        raise FarmConfigError("n_clusters must be >= 1")
    if cfg.services_per_cluster < 2:
        raise FarmConfigError(
            "services_per_cluster must be >= 2, otherwise no cluster has a mate and "
            "the correlation experiment has no positive class"
        )
    if cfg.personas_per_cluster < 1:
        raise FarmConfigError("personas_per_cluster must be >= 1")
    if cfg.n_clean_control_services < 1:
        raise FarmConfigError(
            "n_clean_control_services must be >= 1, otherwise the rule false-positive "
            "rate has no denominator"
        )
    if cfg.target_flaw_count != len(SINGLE_FLAWS) + len(CLUSTER_FLAWS):
        raise FarmConfigError(
            f"target_flaw_count={cfg.target_flaw_count} does not match the catalogue "
            f"({len(SINGLE_FLAWS)} single + {len(CLUSTER_FLAWS)} cluster); the recall "
            "denominator would be wrong"
        )


# --------------------------------------------------------------------------- #
# Services and clusters
# --------------------------------------------------------------------------- #


def _build_services(
    rng: random.Random,
    cfg: FarmConfig,
) -> tuple[list[Cluster], list[Service]]:
    """Create clustered services plus the singletons that serve as clean controls.

    The singletons do double duty and that is deliberate (Part D1/D4): having no
    cluster-mate makes them the correlation *abstention* fixtures, and carrying no
    flaws makes them the rule *false-positive* denominator.
    """
    clusters: list[Cluster] = []
    services: list[Service] = []
    topic_index = 0

    for ci in range(cfg.n_clusters):
        cluster_id = f"C{ci}"
        cluster = Cluster(cluster_id=cluster_id)
        for si in range(cfg.services_per_cluster):
            service_id = f"svc-c{ci}-{si}"
            topic = _TOPICS[topic_index % len(_TOPICS)]
            topic_index += 1
            svc = make_service(rng, cfg.seed, service_id, cluster_id, topic)
            services.append(svc)
            cluster.service_ids.append(service_id)
        clusters.append(cluster)

    for i in range(cfg.n_singleton_services):
        service_id = f"svc-solo-{i}"
        topic = _TOPICS[topic_index % len(_TOPICS)]
        topic_index += 1
        services.append(
            make_service(rng, cfg.seed, service_id, None, topic)
        )

    return clusters, services


def _singleton_ids(cfg: FarmConfig) -> list[str]:
    return [f"svc-solo-{i}" for i in range(cfg.n_singleton_services)]


def _flawable_services(cfg: FarmConfig, services: list[Service]) -> list[Service]:
    """Services that may receive flaws: everything except the clean controls."""
    clean = set(_singleton_ids(cfg))
    return [s for s in services if s.service_id not in clean]


# --------------------------------------------------------------------------- #
# Flaw injection
# --------------------------------------------------------------------------- #


def _inject_single_flaws(
    cfg: FarmConfig,
    services: list[Service],
) -> list:
    """Deal every SINGLE-scope flaw to exactly one service, round-robin."""
    targets = _flawable_services(cfg, services)
    flaws = []
    for index, spec in enumerate(SINGLE_FLAWS):
        svc = targets[index % len(targets)]
        inject_single_flaw(svc, spec, cfg.seed)
        flaw = make_flaw_record(f"FL-{spec.kind}", spec, [svc.service_id])
        svc.flaws.append(flaw.flaw_id)
        flaws.append(flaw)
    return flaws


def _inject_cluster_flaws(
    cfg: FarmConfig,
    clusters: list[Cluster],
    services: list[Service],
) -> list:
    """Give each cluster its own set of shared artefacts -- the genuine links."""
    by_id = {s.service_id: s for s in services}
    flaws = []

    for index, spec in enumerate(CLUSTER_FLAWS):
        cluster = clusters[index % len(clusters)]
        artifact_key = CLUSTER_FLAW_ARTIFACT_KEY[spec.kind]
        value = None
        for service_id in cluster.service_ids:
            svc = by_id[service_id]
            value = assign_cluster_artifact(svc, spec, cfg.seed)
            # Most cluster flaws are artefact-only (a shared favicon hash needs no
            # page). The 404 fingerprint is the exception: it needs a served document
            # whose bytes can actually be hashed.
            if spec.kind in FIXTURE_BUILDERS:
                inject_single_flaw(svc, spec, cfg.seed)
        # Keyed by ARTEFACT key, matching Service.shared_artifacts, so the cluster
        # record and the per-service records are directly comparable. Keying this by
        # flaw kind instead is an easy and silent way to make the correlator's ground
        # truth unreachable.
        cluster.shared_artifacts[artifact_key] = value or ""
        flaw = make_flaw_record(f"FL-{spec.kind}", spec, list(cluster.service_ids))
        for service_id in cluster.service_ids:
            by_id[service_id].flaws.append(flaw.flaw_id)
        flaws.append(flaw)

    return flaws


def widespread_targets(
    eligible_by_cluster: dict[str, list[Service]], share: float
) -> list[Service]:
    """Which eligible services carry a widespread default, at a given prevalence.

    Dealing round-robin across clusters rather than taking a prefix, because a prefix would put
    the whole default inside the alphabetically first clusters and turn a population-wide
    artefact into a cluster-local one -- which is the exact false link the farm's ground truth
    must never contain. ``share >= 1.0`` returns everyone, so the designed farm is unchanged.
    """

    if share >= 1.0:
        # Every eligible service, in the same cluster-major order the farm always used, so the
        # designed farm stays byte-identical.
        return [
            service
            for cluster_id in sorted(eligible_by_cluster)
            for service in eligible_by_cluster[cluster_id]
        ]

    queues = [list(group) for _, group in sorted(eligible_by_cluster.items())]
    ordered: list[Service] = []
    depth = 0
    while any(depth < len(queue) for queue in queues):
        for queue in queues:
            if depth < len(queue):
                ordered.append(queue[depth])
        depth += 1

    # Two is the floor: one service carrying a value is not a shared artefact at all, and the
    # coincidental class already covers exactly-two.
    count = min(len(ordered), max(2, round(share * len(ordered))))
    return ordered[:count]


def _inject_stock_defaults(
    cfg: FarmConfig,
    services: list[Service],
) -> list:
    """Plant vendor-default artefacts, in two distinct classes.

    Both are realistic, and they need **different defences**, which is why both exist:

      * **widespread defaults** -- a stock CMS favicon on every service that does not
        carry a genuine one. This is the common real-world case: a stock artefact
        appears across a large fraction of the population. It is detectable by
        frequency, because a value shared by more services than a cluster cannot be
        cluster-specific.

      * **coincidental defaults** -- the same vendor-default artefact on exactly two
        services in different clusters. This is the rare case, and frequency cannot
        detect it: two is *fewer* services than a genuine cluster, so any frequency
        filter that removes it would also remove real links. Catching this needs a
        blocklist of known default artefacts, not a counting heuristic.

    An earlier version planted only the coincidental class. Naive and
    frequency-filtered correlators then scored identically, so the farm could not
    distinguish them and the correlation metric demonstrated nothing. Splitting the
    classes restores that separation -- and reports honestly that filtering solves one
    of the two problems and not the other.
    """
    from farm.model import HardNegative

    hard_negatives: list[HardNegative] = []

    for index, spec in enumerate(CLUSTER_FLAWS):
        artifact_key = CLUSTER_FLAW_ARTIFACT_KEY[spec.kind]

        eligible_by_cluster: dict[str, list[Service]] = {}
        for svc in services:
            if svc.cluster_id is None:
                continue
            if artifact_key in svc.shared_artifacts:
                continue
            eligible_by_cluster.setdefault(svc.cluster_id, []).append(svc)

        if index < N_WIDESPREAD_DEFAULT_PLANTS:
            # At share 1.0 this is every eligible service: the value becomes conspicuously
            # common, which is what gives frequency filtering something to detect. At a lower
            # share it is still population-wide, just at a rate somebody measured.
            targets = widespread_targets(eligible_by_cluster, cfg.widespread_default_share)
            for svc in targets:
                assign_stock_default(svc, spec)
                hard_negatives.append(
                    HardNegative(
                        fixture_id=f"HN-wide-{spec.kind}-{svc.service_id}",
                        flaw_kind=spec.kind,
                        service_id=svc.service_id,
                        path=spec.path,
                        why=(
                            get_spec(spec.kind).hard_negative_why or ""
                        )
                        + " [widespread: detectable by artefact frequency, "
                        + f"{cfg.widespread_default_share:.1%} of eligible services]",
                    )
                )
            continue

        if index >= N_WIDESPREAD_DEFAULT_PLANTS + N_COINCIDENTAL_DEFAULT_PLANTS:
            # Remaining kinds are left unplanted. The two classes above are the ones
            # the evaluation needs; every kind carrying a default would make the
            # negative class so large that its composition stopped being interesting.
            continue

        # Coincidental class: exactly two services, in different clusters.
        chosen = sorted(eligible_by_cluster)[:2]
        if len(chosen) < 2:
            continue
        for cluster_id in chosen:
            svc = eligible_by_cluster[cluster_id][0]
            assign_stock_default(svc, spec)
            hard_negatives.append(
                HardNegative(
                    fixture_id=f"HN-coincident-{spec.kind}-{svc.service_id}",
                    flaw_kind=spec.kind,
                    service_id=svc.service_id,
                    path=spec.path,
                    why=(
                        get_spec(spec.kind).hard_negative_why or ""
                    )
                    + " [coincidental: frequency filtering cannot detect this]",
                )
            )

    return hard_negatives


# --------------------------------------------------------------------------- #
# Personas
# --------------------------------------------------------------------------- #


def _build_subject_personas(
    rng: random.Random,
    cfg: FarmConfig,
    clusters: list[Cluster],
    services: list[Service],
) -> tuple[list, list[str]]:
    """One subject persona per (cluster, index) slot.

    Returns the personas and a parallel list of the service each persona posts on.
    """
    by_id = {s.service_id: s for s in services}
    template_ids = list(LISTING_TEMPLATES)
    personas = []
    service_ids: list[str] = []

    for ci, cluster in enumerate(clusters):
        for pi in range(cfg.personas_per_cluster):
            service_id = cluster.service_ids[pi % len(cluster.service_ids)]
            svc = by_id[service_id]
            style = _STYLE_CYCLE[pi % len(_STYLE_CYCLE)]
            function_set = _FUNCTION_SET_CYCLE[(ci + pi) % len(_FUNCTION_SET_CYCLE)]

            # Template assignment depends on pi ONLY, never on ci. That is what makes
            # the same template appear in different clusters -- the cross-cluster
            # template match is a hard negative for the correlator, and it is the
            # mechanism that shows template reuse is a linking signal that is not
            # evidence of a shared operator.
            template_id = template_ids[pi % len(template_ids)]

            persona = build_persona(
                rng=rng,
                persona_id=f"{cluster.cluster_id}-P{pi}",
                cluster_id=cluster.cluster_id,
                role=PersonaRole.SUBJECT,
                style=style,
                topic=svc.topic,
                handle=make_handle(rng, cfg.seed, f"{cluster.cluster_id}-P{pi}"),
                function_set=function_set,
                template_id=template_id,
            )
            cluster.persona_ids.append(persona.persona_id)
            personas.append(persona)
            service_ids.append(service_id)

    return personas, service_ids


def _build_near_miss_decoys(
    rng: random.Random,
    cfg: FarmConfig,
    subjects: list,
    services: list[Service],
) -> tuple[list, list[str]]:
    """Decoys that *look like* a subject but are a different identity.

    The configuration is deliberately hard, and that is the point (Part D4):

      * the decoy copies its target's **function-word fingerprint exactly**, so a
        text-only stylometry model has almost no signal to separate them;
      * the decoy posts in **different hours**, so a behavioural model can separate
        them.

    That asymmetry is what lets the harness answer the plan's E3 question ("do
    behavioural features beat lexical features on short posts?") with a measurement
    rather than an assumption. If the system merges these pairs, the false-merge rate
    is real, measured, and not an artefact of easy negatives.
    """
    from farm.model import PersonaRole

    if not subjects:
        return [], []

    # Service each subject posts on, derived from the subject's cluster position.
    decoys = []
    decoy_service_ids: list[str] = []

    for i in range(cfg.n_near_miss_decoys):
        # Spread the decoys across clusters -- one target per cluster -- so a single
        # cluster's writing habit is not doing all the work in the decoy set.
        target_index = (i % cfg.n_clusters) * cfg.personas_per_cluster
        target = subjects[target_index % len(subjects)]
        target_service = None
        for s in services:
            if s.cluster_id == target.cluster_id:
                target_service = s
                break

        topic = target_service.topic if target_service else _TOPICS[0]
        handle = make_handle(rng, cfg.seed, f"decoy{i}", near=target.handle)

        decoy = build_persona(
            rng=rng,
            persona_id=f"DECOY-{i:02d}",
            cluster_id=None,
            role=PersonaRole.NEAR_MISS_DECOY,
            style=target.style,
            topic=topic,
            handle=handle,
            function_set="casual",
        )
        # Textual near-identity: copy the fingerprint wholesale.
        decoy.function_words = list(target.function_words)
        # Behavioural separation: shift the posting window well away from the target.
        target_hours = set(target.active_hours or [0])
        shifted = [((h + 12) % 24) for h in sorted(target_hours)] or [12]
        decoy.active_hours = shifted

        decoys.append(decoy)
        decoy_service_ids.append(target_service.service_id if target_service else "")

    return decoys, decoy_service_ids


# --------------------------------------------------------------------------- #
# Abstention cases
# --------------------------------------------------------------------------- #


def _inject_rule_hard_negatives(
    services: list[Service],
) -> list:
    """Plant a pattern-without-meaning fixture for each rule that has one.

    Each fixture goes on a service that does **not** carry that flaw, so a rule firing
    on it is unambiguously a false positive rather than a correct detection the truth
    happened not to record.

    Services with the fewest existing flaws are used first, which puts most fixtures on
    the singleton clean controls. That is deliberate: those services are supposed to be
    free of flaws, so they are exactly where an over-eager rule should be caught.
    """
    from farm.model import HardNegative
    from farm.services import HARD_NEGATIVE_FIXTURES, inject_rule_hard_negative

    # Which services already carry each flaw kind.
    carriers: dict[str, set[str]] = {}
    for svc in services:
        for flaw_id in svc.flaws:
            kind = flaw_id[len("FL-"):] if flaw_id.startswith("FL-") else flaw_id
            carriers.setdefault(kind, set()).add(svc.service_id)

    # Fewest flaws first, then stable by id so the farm stays reproducible.
    ordered = sorted(services, key=lambda s: (len(s.flaws), s.service_id))

    hard_negatives = []
    for kind in sorted(HARD_NEGATIVE_FIXTURES):
        blocked = carriers.get(kind, set())
        target = next((s for s in ordered if s.service_id not in blocked), None)
        if target is None:  # pragma: no cover - needs every service to carry the flaw
            continue
        if not inject_rule_hard_negative(target, kind):  # pragma: no cover
            continue
        path = HARD_NEGATIVE_FIXTURES[kind][0]
        hard_negatives.append(
            HardNegative(
                fixture_id=f"HN-rule-{kind}-{target.service_id}",
                flaw_kind=kind,
                service_id=target.service_id,
                path=path,
                why=get_spec(kind).hard_negative_why or "",
            )
        )
    return hard_negatives


def _assign_persona_artifacts(
    cfg: FarmConfig,
    clusters: list[Cluster],
    subjects: list,
    decoys: list,
) -> None:
    """Give personas the artefact set an entity-resolution step will extract.

    Two link kinds are generated, and the second is the point:

      * **true links** -- within each cluster, one persona pair shares a PGP
        fingerprint and another shares a wallet address. These are the merges the
        system should find.
      * **spurious links** -- every decoy carries the same widely-published
        placeholder handle. A naive exact-match rule merges all of them, and every
        one of those merges is false, because a shared public placeholder is not
        evidence of a shared operator.

    Without the spurious half, the false-merge rate is measured only against pairs
    the rule already got right, which is the defect Part E2 exists to avoid.
    """
    from farm.model import PUBLIC_PLACEHOLDER_ARTIFACT, PERSONA_ARTIFACT_KEYS

    # --- true links: within-cluster shared artefacts ------------------------- #
    pair_plan = ((0, 1, "pgp_fingerprint"), (2, 3, "wallet_address"))
    for cluster in clusters:
        members = [p for p in subjects if p.cluster_id == cluster.cluster_id]
        for first, second, key in pair_plan:
            if first >= len(members) or second >= len(members):
                continue
            value = deterministic_hex(
                cfg.seed, f"persona-artifact:{key}:{cluster.cluster_id}", length=40
            )
            members[first].shared_artifacts[key] = value
            members[second].shared_artifacts[key] = value

    # --- spurious links: the widely-published placeholder ------------------- #
    for decoy in decoys:
        decoy.shared_artifacts["contact_handle"] = PUBLIC_PLACEHOLDER_ARTIFACT

    # Every persona carries a distinct handle for its unused artefact keys, so the
    # absence of a key never accidentally reads as a match.
    for persona in subjects:
        for key in PERSONA_ARTIFACT_KEYS:
            if key not in persona.shared_artifacts:
                persona.shared_artifacts[key] = deterministic_hex(
                    cfg.seed, f"persona-artifact:{key}:{persona.persona_id}", length=40
                )


def _build_abstention_cases(
    cfg: FarmConfig,
    services: list[Service],
    subjects: list,
    decoys: list,
) -> list[AbstentionCase]:
    """Queries whose correct answer is "not present in the pool".

    Two kinds, matching the two ways the system can be asked a question it cannot
    answer (Part D4):

      * **correlation** -- the query service has no cluster-mate, so there is no
        service to link it to. The singletons supply these.
      * **retrieval** -- the query text's author is not in the candidate pool. Built
        by querying with a decoy's text against a pool that contains only subjects,
        so the decoy is absent *by construction* rather than by luck.

    A system scored only on answerable queries can never be measured on restraint,
    and restraint is the safety property that matters most for a tool that proposes
    candidate identities.
    """
    cases: list[AbstentionCase] = []

    # Correlation abstention: the singletons.
    subject_ids = [p.persona_id for p in subjects]
    for svc in services:
        if svc.cluster_id is not None:
            continue
        cases.append(
            AbstentionCase(
                case_id=f"ABS-CORR-{svc.service_id}",
                kind="correlation",
                query=svc.service_id,
                pool=[s.service_id for s in services if s.service_id != svc.service_id],
                why_absent=(
                    "Singleton service: it has no cluster-mate, so no candidate in the "
                    "pool is a true link. Any non-abstaining answer is a false merge."
                ),
            )
        )

    # Retrieval abstention: decoy text against a subjects-only pool.
    for decoy in decoys[: cfg.n_abstention_retrieval_cases]:
        cases.append(
            AbstentionCase(
                case_id=f"ABS-RETR-{decoy.persona_id}",
                kind="retrieval",
                query=decoy.posts[0].post_id if decoy.posts else decoy.persona_id,
                pool=list(subject_ids),
                why_absent=(
                    f"Query author {decoy.persona_id} is not in the candidate pool by "
                    "construction. A correct system abstains; returning a top-1 is a "
                    "false association."
                ),
            )
        )

    return cases


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def assert_every_regime_is_instantiated(personas) -> None:
    """Every style the farm declares must appear in the farm it generates.

    This is not a completeness nicety. `HINGLISH_HEAVY` was declared in `StyleRegime` and never
    instantiated, because the style cycle assigned slots by index and the regime sat past the last
    persona slot in a cluster. Nothing noticed for the life of the project: the code-mix block was
    scored on light mixing alone while the report called it the differentiator. A regime with no
    personas is a claim with no data behind it, so the generator refuses to produce one.
    """
    built = {p.style for p in personas}
    missing = [r.value for r in StyleRegime if r not in built]
    if missing:
        raise FarmConfigError(
            f"style regime(s) {missing} are declared but no persona was generated with them; "
            f"either widen n_personas_per_cluster to cover the style cycle "
            f"({len(_STYLE_CYCLE)} slots) or remove the regime from StyleRegime"
        )


def generate_farm(cfg: FarmConfig | None = None) -> FarmTruth:
    """Build a complete, deterministic synthetic farm.

    Same config -> identical farm, byte for byte. That property is what lets the
    report cite a seed and a command instead of shipping a frozen fixture set, and
    it is what makes every Part E number reproducible by a third party.
    """
    cfg = cfg or FarmConfig()
    validate_config(cfg)
    # (the persona regimes are checked once they exist, below)

    rng = random.Random(cfg.seed)

    clusters, services = _build_services(rng, cfg)

    flaws = _inject_single_flaws(cfg, services)
    flaws += _inject_cluster_flaws(cfg, clusters, services)
    hard_negatives = _inject_stock_defaults(cfg, services)
    hard_negatives += _inject_rule_hard_negatives(services)

    subjects, subject_service_ids = _build_subject_personas(rng, cfg, clusters, services)
    decoys, decoy_service_ids = _build_near_miss_decoys(rng, cfg, subjects, services)
    _assign_persona_artifacts(cfg, clusters, subjects, decoys)

    all_personas = subjects + decoys
    all_service_ids = subject_service_ids + decoy_service_ids

    topics = list(TOPIC_VOCABULARY)
    for persona, service_id in zip(all_personas, all_service_ids, strict=True):
        # Style drift on some personas: content vocabulary moves, function words do
        # not (D3). The drift topic is deliberately different from the persona's own.
        own_topic = None
        for s in services:
            if s.service_id == service_id:
                own_topic = s.topic
                break
        drift_topic = next((t for t in topics if t != own_topic), topics[0])
        drift_at = 4 if persona.style is not StyleRegime.TEMPLATE_REUSER else None

        # Record the drift on the model so the harness can test a *query* against the
        # boundary rather than re-deriving it from post counts. `drift_topic` is only
        # recorded when drift actually happens, so a None topic means "no drift".
        persona.drift_at = drift_at
        persona.drift_topic = drift_topic if drift_at is not None else None

        persona.posts = generate_posts(
            rng=rng,
            persona=persona,
            service_id=service_id or None,
            post_count=cfg.posts_per_persona,
            drift_at=drift_at,
            drift_topic=drift_topic,
        )

    abstention_cases = _build_abstention_cases(cfg, services, subjects, decoys)

    # Checked over every persona the farm contains, subjects and decoys, because a regime that only
    # exists on a decoy is still a regime the report will claim it measured.
    assert_every_regime_is_instantiated(list(subjects) + list(decoys))

    return FarmTruth(
        config=cfg,
        clusters=clusters,
        services=services,
        personas=all_personas,
        flaws=flaws,
        hard_negatives=hard_negatives,
        abstention_cases=abstention_cases,
    )