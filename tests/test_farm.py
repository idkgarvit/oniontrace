"""Tests for the synthetic farm generator.

These are not decoration. Each test locks in a property that the evaluation in Part E
depends on -- if one of them fails, some number in the report becomes unfalsifiable,
inflated, or unmeasurable. The farm's own `validate` command checks the same
invariants at runtime; these tests check them exhaustively.
"""

from __future__ import annotations

import random

import pytest

from farm.catalogue import (
    ALL_FLAWS,
    CLUSTER_FLAWS,
    SINGLE_FLAWS,
    catalogue_summary,
    rules_without_hard_negative,
)
from farm.generator import FarmConfigError, generate_farm, validate_config
from farm.language import LISTING_TEMPLATES, TOPIC_VOCABULARY
from farm.model import FarmConfig, FlawScope, PersonaRole, StyleRegime
from farm.personas import build_persona, generate_posts
from farm.services import (
    CLUSTER_FLAW_ARTIFACT_KEY,
    HEADER_FLAWS,
    STOCK_DEFAULT_ARTIFACT,
    unbuildable_flaw_kinds,
)


@pytest.fixture(scope="session")
def farm():
    """The default farm. Session-scoped: it is deterministic, so one build suffices."""
    return generate_farm()


# --------------------------------------------------------------------------- #
# Catalogue integrity
# --------------------------------------------------------------------------- #


def test_catalogue_has_expected_shape():
    summary = catalogue_summary()
    assert summary["total"] == 25, "Part D2 states 25 injected flaws"
    assert summary["single"] + summary["cluster"] == summary["total"]
    assert summary["cluster"] >= 1, "the correlator needs a positive class"


def test_every_flaw_kind_is_unique():
    kinds = [f.kind for f in ALL_FLAWS]
    assert len(kinds) == len(set(kinds))


def test_rules_without_a_hard_negative_are_a_known_short_list():
    """Part D4: a rule with no negative class has an unfalsifiable precision number.

    This test previously asserted `rules_without_hard_negative() == []`, i.e. that every
    rule was protected. It passed only because the function it checked was broken: it
    tested whether the `hard_negative_why` prose field was populated, and all 25 flaws
    carry that field, so it always returned an empty list. Seven single-scope rules had a
    rationale for a negative but no fixture served, and their precision was measured against
    true positives only.

    Those seven are now implemented, so the list is empty again, and this time it is empty
    because the fixtures exist. The test checks both directions, because an empty list is
    exactly what the broken function used to return: the list must be empty AND every rule
    that can be scored must have a fixture behind it.
    """
    from eval.detectors.misconfig import RULES
    from farm.services import HARD_NEGATIVE_FIXTURES

    gap = rules_without_hard_negative()
    assert gap == [], f"rules without a negative fixture: {gap}"
    # The two views must not disagree, which is what let the defect hide.
    import eval.detectors.misconfig as misconfig

    from farm.services import rules_with_hard_negative_fixture

    assert len(gap) + len(rules_with_hard_negative_fixture()) == len(SINGLE_FLAWS)
    assert set(rules_with_hard_negative_fixture()) == set(RULES)
    assert set(HARD_NEGATIVE_FIXTURES) >= set(misconfig.RULES)


def test_every_flaw_kind_is_buildable():
    """A kind with no fixture path would silently shrink the recall denominator."""
    assert unbuildable_flaw_kinds() == []


def test_hard_negative_text_is_substantive():
    """A hard negative must be explainable; a one-word reason is not a reason."""
    for spec in ALL_FLAWS:
        assert spec.hard_negative_why is not None
        assert len(spec.hard_negative_why) > 40, spec.kind


# --------------------------------------------------------------------------- #
# Determinism
# --------------------------------------------------------------------------- #


def test_same_seed_is_byte_identical():
    """The report cites a seed instead of shipping fixtures, so this must hold."""
    assert generate_farm().to_json() == generate_farm().to_json()


def test_different_seed_differs():
    assert generate_farm(FarmConfig(seed=1)).to_json() != generate_farm(
        FarmConfig(seed=2)
    ).to_json()


def test_generation_does_not_mutate_global_random_state():
    """A generator that seeds the global RNG would make other tests order-dependent."""
    random.seed(1234)
    before = random.random()
    random.seed(1234)
    generate_farm()
    assert random.random() == before


# --------------------------------------------------------------------------- #
# Composition
# --------------------------------------------------------------------------- #


def test_service_composition(farm):
    clustered = [s for s in farm.services if s.cluster_id]
    singletons = [s for s in farm.services if not s.cluster_id]
    assert len(farm.clusters) == farm.config.n_clusters
    assert len(clustered) == farm.config.n_clusters * farm.config.services_per_cluster
    assert len(singletons) == farm.config.n_singleton_services


def test_every_cluster_has_a_mate(farm):
    """With one service per cluster the correlation experiment has no positive class."""
    for svc in farm.services:
        if svc.cluster_id:
            assert farm.cluster_mates(svc.service_id), svc.service_id


def test_flaw_count_matches_catalogue(farm):
    assert len(farm.flaws) == 25
    assert len(farm.flaws_by_scope(FlawScope.SINGLE)) == len(SINGLE_FLAWS)
    assert len(farm.flaws_by_scope(FlawScope.CLUSTER)) == len(CLUSTER_FLAWS)


def test_persona_composition(farm):
    subjects = [p for p in farm.personas if p.role is PersonaRole.SUBJECT]
    decoys = [p for p in farm.personas if p.role is PersonaRole.NEAR_MISS_DECOY]
    assert len(subjects) == farm.config.n_clusters * farm.config.personas_per_cluster
    assert len(decoys) == farm.config.n_near_miss_decoys


def test_persona_ids_are_unique(farm):
    ids = [p.persona_id for p in farm.personas]
    assert len(ids) == len(set(ids))


def test_every_persona_has_posts(farm):
    for persona in farm.personas:
        assert persona.posts, persona.persona_id
        assert persona.active_hours, persona.persona_id


# --------------------------------------------------------------------------- #
# Flaw injection: ground truth must match what is actually on disk
# --------------------------------------------------------------------------- #


def flaw_is_present(farm, flaw) -> bool:
    """True if the fixture for this flaw really exists where the truth says it does.

    If this returns False for any flaw, the recall metric is computed against a
    denominator that includes flaws that were never actually injected -- i.e. the
    reported recall is wrong and would be wrong in the flattering direction.
    """
    from farm.catalogue import FLAWS_BY_KIND

    spec = FLAWS_BY_KIND[flaw.kind]
    artifact_key = CLUSTER_FLAW_ARTIFACT_KEY.get(flaw.kind)

    for service_id in flaw.service_ids:
        svc = farm.service(service_id)
        if artifact_key and artifact_key in svc.shared_artifacts:
            return True
        header_set = HEADER_FLAWS.get(flaw.kind)
        if header_set:
            index = svc.endpoint("/")
            if index and all(index.headers.get(k) == v for k, v in header_set.items()):
                return True
        if svc.endpoint(spec.path) is not None:
            return True
    return False


def test_every_ground_truth_flaw_is_actually_injected(farm):
    missing = [f.flaw_id for f in farm.flaws if not flaw_is_present(farm, f)]
    assert missing == [], f"ground truth claims flaws that were not injected: {missing}"


def test_clean_control_services_are_genuinely_clean(farm):
    """These are the rule false-positive denominator; a stray flaw corrupts it."""
    clean = farm.clean_services()
    assert clean, "no clean control services"
    for svc in clean:
        assert svc.flaws == [], svc.service_id
        assert svc.cluster_id is None, "a clustered service cannot be a clean control"


def test_clean_control_endpoints_exist_inside_flawed_services(farm):
    """Part D1: FP must be measured on clean endpoints *within* flawed services."""
    flawed_ids = {s.service_id for s in farm.services if s.flaws}
    endpoints = farm.clean_control_endpoints()
    assert endpoints
    assert any(sid in flawed_ids for sid, _ in endpoints)


def test_cluster_artifacts_are_keyed_by_artefact_key(farm):
    """Cluster and service records must use the same keys, or the truth is unreachable."""
    for cluster in farm.clusters:
        for key, value in cluster.shared_artifacts.items():
            for service_id in cluster.service_ids:
                svc = farm.service(service_id)
                assert svc.shared_artifacts.get(key) == value, (
                    f"{cluster.cluster_id}.{key} does not match on {service_id}"
                )


def test_shared_404_body_is_cluster_specific(farm):
    """Served bytes must be identical within a cluster and different between clusters.

    A stock page served to every service would be shared farm-wide, so a hash-matching
    rule would link the entire farm and the fixture would measure nothing.
    """
    by_cluster: dict[str, set[str]] = {}
    for svc in farm.services:
        if not svc.cluster_id:
            continue
        endpoint = svc.endpoint("/definitely-not-here")
        if endpoint is None:
            continue
        by_cluster.setdefault(svc.cluster_id, set()).add(endpoint.body_sha256())

    assert by_cluster, "no 404 fixtures found"
    for cluster_id, hashes in by_cluster.items():
        assert len(hashes) == 1, f"{cluster_id} services serve differing 404 bodies"

    representatives = [next(iter(h)) for h in by_cluster.values()]
    assert len(set(representatives)) == len(representatives), (
        "two clusters serve a byte-identical 404 page, creating an unlabelled link"
    )


def test_clean_control_endpoints_are_not_flaw_paths(farm):
    from farm.catalogue import FLAWS_BY_KIND

    flaw_paths = {FLAWS_BY_KIND[f.kind].path for f in farm.flaws}
    for _sid, endpoint in farm.clean_control_endpoints():
        assert endpoint.path not in flaw_paths, endpoint.path


def test_flawed_services_carry_at_least_one_flaw_id(farm):
    for svc in farm.services:
        if svc.cluster_id is not None:
            assert svc.flaws, f"clustered service {svc.service_id} has no flaw ids"


def test_header_flaws_are_attached_to_the_index(farm):
    kinds = {f.kind for f in farm.flaws}
    for kind in kinds & set(HEADER_FLAWS):
        spec_headers = HEADER_FLAWS[kind]
        holder = None
        for svc in farm.services:
            index = svc.endpoint("/")
            if index and all(index.headers.get(k) == v for k, v in spec_headers.items()):
                holder = svc.service_id
                break
        assert holder is not None, f"{kind} header never attached"


# --------------------------------------------------------------------------- #
# Safety properties of the fixtures themselves
# --------------------------------------------------------------------------- #


_SECRET_KEY_MARKERS = ("password", "passwd", "pwd", "key", "secret", "token")


def test_dot_env_fixture_contains_no_credentials(farm):
    """The decoy must never contain a populated credential, even a synthetic one.

    Non-secret placeholders (APP_NAME=example) are intentional: they make the fixture
    realistic. What must never appear is a secret with a value, because then the farm
    would be shipping something that looks like a leaked credential.
    """
    harmless_placeholders = {"example", "local", "dev", "test", ""}
    checked = 0
    for svc in farm.services:
        endpoint = svc.endpoint("/.env")
        if endpoint is None:
            continue
        for line in endpoint.body.splitlines():
            if "=" not in line:
                continue
            checked += 1
            key, _, value = line.partition("=")
            value = value.strip()
            if any(marker in key.lower() for marker in _SECRET_KEY_MARKERS):
                assert value == "", f"decoy .env carries a populated credential: {line!r}"
            else:
                assert value in harmless_placeholders, f"unexpected value: {line!r}"
    assert checked > 0, "no .env fixture found to check"


def test_exif_fixture_is_metadata_only(farm):
    """Media is never fetched on live collection; the fixture must reflect that."""
    for svc in farm.services:
        endpoint = svc.endpoint("/assets/img/product-1.jpg")
        if endpoint is None:
            continue
        assert "JPEG fixture" in endpoint.body
        assert "GPSLatitude" in endpoint.body
        # A metadata stub, not real image bytes.
        assert len(endpoint.body) < 500


# --------------------------------------------------------------------------- #
# Correlation ground truth: genuine links and the false links that measure precision
# --------------------------------------------------------------------------- #


def test_cluster_mates_share_identical_artifacts(farm):
    """The correlator's positive class must be real: same value inside a cluster."""
    for cluster in farm.clusters:
        members = [farm.service(sid) for sid in cluster.service_ids]
        for key in cluster.shared_artifacts:
            values = {m.shared_artifacts.get(key) for m in members}
            assert len(values) == 1, f"{cluster.cluster_id}.{key} differs across mates"
            assert None not in values, f"{cluster.cluster_id}.{key} missing on a mate"


def test_clusters_do_not_share_artifact_values(farm):
    """Two clusters sharing a value would be an unlabelled false link in the truth."""
    seen: dict[str, str] = {}
    for cluster in farm.clusters:
        for key, value in cluster.shared_artifacts.items():
            assert value != STOCK_DEFAULT_ARTIFACT
            if key in seen:
                assert seen[key] != value, (
                    f"clusters share the same {key} value, creating an unlabelled "
                    "false link in the ground truth"
                )
            seen[key] = value


def test_stock_defaults_span_different_clusters(farm):
    """The false-similarity mechanism: same default value in unrelated clusters.

    Without this, a correlator's precision is measured only against services it
    already got right, which inflates it (Part D4).
    """
    by_key: dict[str, set[str]] = {}
    for svc in farm.services:
        for key, value in svc.shared_artifacts.items():
            if value == STOCK_DEFAULT_ARTIFACT:
                by_key.setdefault(key, set()).add(svc.cluster_id or "solo")

    assert by_key, "no stock-default artefacts planted: no spurious links exist"
    for key, clusters in by_key.items():
        assert len(clusters) >= 2, (
            f"stock default for {key} appears in only {clusters}; it cannot create a "
            "cross-cluster false merge"
        )


def _stock_default_groups(farm) -> dict[str, set[str]]:
    """Map artefact key -> services carrying the stock-default value for it."""
    grouped: dict[str, set[str]] = {}
    for svc in farm.services:
        for key, value in svc.shared_artifacts.items():
            if value == STOCK_DEFAULT_ARTIFACT:
                grouped.setdefault(key, set()).add(svc.service_id)
    return grouped


def test_both_default_classes_exist(farm):
    """A farm with only one class cannot distinguish a careful correlator from a
    careless one, so the correlation metric would demonstrate nothing."""
    per_cluster = farm.config.services_per_cluster
    grouped = _stock_default_groups(farm)
    wide = {k for k, v in grouped.items() if len(v) > per_cluster}
    narrow = {k for k, v in grouped.items() if len(v) <= per_cluster}
    assert wide, "no widespread defaults: frequency filtering has nothing to catch"
    assert narrow, "no coincidental defaults: the frequency-proof case is absent"


def test_widespread_defaults_are_frequency_detectable(farm):
    """A widespread default must appear on more services than a genuine cluster.

    That is precisely what makes it separable by counting. If it appeared on fewer,
    no frequency heuristic could tell it apart from a real shared artefact.
    """
    per_cluster = farm.config.services_per_cluster
    wide = {
        key: members
        for key, members in _stock_default_groups(farm).items()
        if len(members) > per_cluster
    }
    assert wide, "no stock default exceeds cluster size"
    for key, members in wide.items():
        assert len(members) > per_cluster, key


def test_coincidental_defaults_are_rare_and_cross_cluster(farm):
    """The case frequency filtering cannot solve: rare, but genuinely spurious.

    Two services sharing a vendor-default artefact are not one operator, and because
    two is fewer than a cluster, any filter that removed them would also remove real
    links. Catching these needs a blocklist of known defaults.
    """
    per_cluster = farm.config.services_per_cluster
    narrow = {
        key: members
        for key, members in _stock_default_groups(farm).items()
        if len(members) <= per_cluster
    }
    assert narrow, "no coincidental defaults planted"
    for key, members in narrow.items():
        clusters = {farm.service(s).cluster_id for s in members}
        assert len(clusters) >= 2, (
            f"{key} coincides within a single cluster {clusters}, where it would be "
            "indistinguishable from a genuine artefact"
        )


def test_hard_negatives_reference_real_services(farm):
    assert farm.hard_negatives
    for hn in farm.hard_negatives:
        assert farm.service(hn.service_id) is not None
        assert hn.why, "a hard negative without a reason is not a hard negative"


def test_hard_negative_kinds_exist_in_the_catalogue(farm):
    from farm.catalogue import FLAWS_BY_KIND

    for hn in farm.hard_negatives:
        assert hn.flaw_kind in FLAWS_BY_KIND


def test_only_clustered_services_have_cluster_artifacts(farm):
    for svc in farm.services:
        if not svc.cluster_id:
            assert svc.shared_artifacts == {}, (
                f"singleton {svc.service_id} carries cluster artefacts, which would "
                "make it non-clean as an FP control"
            )


def test_template_reuse_spans_clusters(farm):
    """D3's known-hard case: the same template in unrelated clusters.

    A template-match rule links these personas, and those links are false. This test
    guarantees the confound is present rather than accidental.
    """
    by_template: dict[str, set[str]] = {}
    for persona in farm.personas:
        if persona.template_id:
            by_template.setdefault(persona.template_id, set()).add(
                persona.cluster_id or "none"
            )
    shared = {t: c for t, c in by_template.items() if len(c) >= 2}
    assert shared, "no template is reused across clusters: the hard case is absent"


def test_all_templates_used_are_registered(farm):
    for persona in farm.personas:
        if persona.template_id is not None:
            assert persona.template_id in LISTING_TEMPLATES


# --------------------------------------------------------------------------- #
# Decoys: textually near-identical, behaviourally distinct
# --------------------------------------------------------------------------- #


def _subject_fingerprints(farm) -> dict:
    return {
        tuple(p.function_words): p
        for p in farm.personas
        if p.role is PersonaRole.SUBJECT
    }


def _decoys(farm) -> list:
    return [p for p in farm.personas if p.role is PersonaRole.NEAR_MISS_DECOY]


def test_decoys_copy_their_target_fingerprint(farm):
    """Makes the decoy a genuine near-miss: text alone should not separate them."""
    fingerprints = set(_subject_fingerprints(farm))
    assert fingerprints, "no subject fingerprints to match against"
    for decoy in _decoys(farm):
        assert tuple(decoy.function_words) in fingerprints, (
            f"{decoy.persona_id} does not share a subject's function-word fingerprint, "
            "so it is not a near-miss and its false-merge rate is not meaningful"
        )


def test_decoys_are_behaviourally_distinct_from_their_target(farm):
    """The other half of the near-miss design: hours must differ, or nothing can."""
    subjects = _subject_fingerprints(farm)
    for decoy in _decoys(farm):
        target = subjects.get(tuple(decoy.function_words))
        assert target is not None
        assert set(decoy.active_hours) != set(target.active_hours), (
            f"{decoy.persona_id} posts in exactly its target's hours, so neither text "
            "nor behaviour can separate them"
        )


def test_decoys_are_not_subjects(farm):
    subject_ids = {p.persona_id for p in farm.personas if p.role is PersonaRole.SUBJECT}
    decoy_ids = {p.persona_id for p in _decoys(farm)}
    assert not (subject_ids & decoy_ids)


def test_decoy_handles_resemble_but_differ_from_targets(farm):
    subjects = _subject_fingerprints(farm)
    for decoy in _decoys(farm):
        target = subjects.get(tuple(decoy.function_words))
        assert target is not None
        assert decoy.handle != target.handle, "a decoy must be a different identity"
        assert decoy.handle.startswith(target.handle), (
            f"decoy handle {decoy.handle!r} should resemble target {target.handle!r}"
        )


def test_decoys_span_different_clusters(farm):
    """Concentrating decoys on one cluster would let one writing habit do all the work."""
    subjects = _subject_fingerprints(farm)
    targets = {
        subjects[tuple(d.function_words)].cluster_id for d in _decoys(farm)
    }
    assert len(targets) >= 2, f"all decoys target clusters {targets}"


# --------------------------------------------------------------------------- #
# Style drift and code-mix control
# --------------------------------------------------------------------------- #


def _test_persona(rng: random.Random, style: StyleRegime, topic: str):
    return build_persona(
        rng=rng,
        persona_id=f"T-{style.value}",
        cluster_id="T",
        role=PersonaRole.SUBJECT,
        style=style,
        topic=topic,
        handle="h",
        function_set="casual",
    )


def test_style_drift_changes_content_but_not_function_words():
    """D3's core requirement, tested directly rather than assumed."""
    rng = random.Random(11)
    persona = _test_persona(rng, StyleRegime.PURE_ENGLISH, "vintage_electronics")
    fingerprint_before = list(persona.function_words)

    drift_topic = "garden_supplies"
    posts = generate_posts(
        rng=rng,
        persona=persona,
        service_id="s",
        post_count=12,
        drift_at=6,
        drift_topic=drift_topic,
    )

    assert persona.function_words == fingerprint_before, "drift altered the fingerprint"

    garden = set(TOPIC_VOCABULARY[drift_topic])
    later = set(" ".join(p.text for p in posts[6:]).split())
    assert garden & later, "drifted posts do not use the drift topic's vocabulary"


def test_drift_ground_truth_is_recorded_on_the_persona(farm):
    """The harness reads drift from these fields, so their absence would be silent.

    The drift breakdown was previously inferred from post count, which produced a table
    that looked like a finding and was an artefact of query length.
    """
    drifted = [p for p in farm.personas if p.drift_at is not None]
    assert drifted, "no persona drifted, so D3 is untested"
    assert len(drifted) < len(farm.personas), "every persona drifted: no control group"

    topics = {s.service_id: s.topic for s in farm.services}
    for persona in drifted:
        assert persona.drift_topic is not None, persona.persona_id
        service_id = next(p.service_id for p in persona.posts if p.service_id)
        assert persona.drift_topic != topics[service_id], (
            f"{persona.persona_id}: drift topic equals the persona's own topic, so "
            "nothing distinguishable changes"
        )


def test_personas_that_never_drift_record_no_drift_topic(farm):
    """A topic recorded for a persona that never drifts reads as a drift that isn't one."""
    for persona in farm.personas:
        if persona.style is not StyleRegime.TEMPLATE_REUSER:
            continue
        assert persona.drift_at is None
        assert persona.drift_topic is None


def test_the_recorded_drift_boundary_is_where_the_vocabulary_changes(farm):
    """The harness trusts `drift_at` to decide which queries count as post-drift.

    If the boundary did not line up with the vocabulary shift, the E3 drift breakdown
    would split queries in the wrong place and quietly measure something else.
    """
    for persona in [p for p in farm.personas if p.drift_at is not None]:
        own = set(persona.content_vocabulary)
        before = {
            word
            for post in persona.posts
            if post.index < persona.drift_at
            for word in post.text.split()
        }
        after = {
            word
            for post in persona.posts
            if post.index >= persona.drift_at
            for word in post.text.split()
        }
        coverage_before = len(own & before) / len(own)
        coverage_after = len(own & after) / len(own)
        assert coverage_after < coverage_before, (
            f"{persona.persona_id}: the persona's own vocabulary is {coverage_after:.2f} "
            f"present after drift_at but {coverage_before:.2f} before, so drift_at is "
            "not the boundary of the vocabulary change"
        )


def test_function_word_sets_are_not_all_identical(farm):
    """If every persona had the same habit words, text-only matching would be chance."""
    subjects = [p for p in farm.personas if p.role is PersonaRole.SUBJECT]
    fingerprints = {tuple(p.function_words) for p in subjects}
    assert len(fingerprints) > 1, "all subjects share one fingerprint"


def test_code_mix_regime_is_respected():
    """Hinglish-heavy text must contain more romanised-Hindi tokens than pure English."""
    from farm.language import HINGLISH_CONTENT_TOKENS, HINGLISH_FUNCTION_WORDS

    hinglish_tokens = set(HINGLISH_CONTENT_TOKENS) | set(HINGLISH_FUNCTION_WORDS)

    def mix_ratio(style: StyleRegime) -> float:
        rng = random.Random(5)
        persona = _test_persona(rng, style, "used_books")
        posts = generate_posts(rng, persona, "s", post_count=8)
        words = " ".join(p.text for p in posts).split()
        hits = sum(1 for w in words if w in hinglish_tokens)
        return hits / max(1, len(words))

    pure = mix_ratio(StyleRegime.PURE_ENGLISH)
    heavy = mix_ratio(StyleRegime.HINGLISH_HEAVY)
    assert pure == 0.0, f"pure-English persona contains code-mix tokens ({pure})"
    assert heavy > 0.10, f"heavy code-mix persona only reached {heavy:.3f}"


def test_post_length_buckets_are_produced():
    rng = random.Random(3)
    persona = _test_persona(rng, StyleRegime.PURE_ENGLISH, "music_gear")
    posts = generate_posts(rng, persona, "s", post_count=30)
    buckets = {p.length_bucket for p in posts}
    assert buckets == {"short", "long"}, f"only produced {buckets}"


def test_short_posts_are_actually_short():
    rng = random.Random(4)
    persona = _test_persona(rng, StyleRegime.PURE_ENGLISH, "board_games")
    posts = generate_posts(rng, persona, "s", post_count=40)
    shorts = [p for p in posts if p.length_bucket == "short"]
    assert shorts, "no short posts produced"
    assert all(p.word_count < 150 for p in shorts)


def test_template_reuser_personas_produce_template_posts(farm):
    """The known-hard case must actually appear in the corpus, or E3 tests nothing."""
    reusers = [p for p in farm.personas
               if p.style is StyleRegime.TEMPLATE_REUSER and p.role is PersonaRole.SUBJECT]
    assert reusers, "no template-reuser personas"
    assert any(p.is_template for r in reusers for p in r.posts)


def test_posting_hours_come_from_the_persona_window(farm):
    """Behavioural ground truth must match the corpus, or that feature has no signal."""
    for persona in farm.personas:
        allowed = set(persona.active_hours)
        used = {p.hour_of_day for p in persona.posts}
        assert used <= allowed, (
            f"{persona.persona_id} posted at hours {sorted(used - allowed)} outside "
            f"its declared window {sorted(allowed)}"
        )


# --------------------------------------------------------------------------- #
# Baselines -- Part E1 requires these to be reported, so they must be correct
# --------------------------------------------------------------------------- #


def test_correlation_baseline_restricted_pool(farm):
    """12 clustered services, 2 true mates among 11 alternatives -> 2/11."""
    assert farm.precision_at_1_baseline() == pytest.approx(2 / 11)


def test_correlation_baseline_full_pool(farm):
    """All services in the pool, singletons included -> 2/13."""
    assert farm.precision_at_1_baseline(restricted_to_clustered=False) == pytest.approx(
        2 / 13
    )


def test_retrieval_baseline_is_k_over_pool(farm):
    pool = len(farm.persona_pool())
    for k in (1, 3, 5, 10):
        payload = farm.retrieval_baselines(k)
        assert payload["pool_size"] == pool
        assert payload["hit_at_k"] == pytest.approx(min(1.0, k / pool))
        assert payload["recall_at_k"] == pytest.approx(min(1.0, k / pool))


def test_retrieval_baseline_is_non_trivial(farm):
    """Guards the defect Part E was rewritten to fix.

    If a top-k baseline is near 1.0 the metric measures nothing. The old plan's
    "short text R@10 > 60%" was satisfied by chance at a 16-persona pool; this asserts
    the pool is large enough that the same cannot happen here.
    """
    assert farm.retrieval_baselines(5)["hit_at_k"] < 0.40
    assert farm.retrieval_baselines(10)["hit_at_k"] < 0.75


def test_correlation_pool_excludes_the_query_service(farm):
    for svc in farm.services:
        pool = farm.correlation_pool(svc.service_id)
        assert svc.service_id not in pool
        assert len(pool) == len(farm.services) - 1


# --------------------------------------------------------------------------- #
# Abstention cases
# --------------------------------------------------------------------------- #


def test_correlation_abstention_covers_every_singleton(farm):
    singletons = {s.service_id for s in farm.services if not s.cluster_id}
    covered = {
        a.query for a in farm.abstention_cases if a.kind == "correlation"
    }
    assert singletons == covered, f"singletons without an abstention case: {singletons - covered}"


def test_correlation_abstention_answer_is_truly_absent(farm):
    for case in farm.abstention_cases:
        if case.kind != "correlation":
            continue
        for candidate in case.pool:
            mates = set(farm.cluster_mates(case.query))
            assert candidate not in mates, (
                f"{candidate} is a true mate of {case.query}, so abstaining would be wrong"
            )


def test_retrieval_abstention_author_is_not_in_the_pool(farm):
    """The whole point: the correct answer is absent by construction."""
    for case in farm.abstention_cases:
        if case.kind != "retrieval":
            continue
        post_owner = None
        for persona in farm.personas:
            if any(p.post_id == case.query for p in persona.posts):
                post_owner = persona.persona_id
                break
        assert post_owner is not None, f"abstention query {case.query} matches no post"
        assert post_owner not in case.pool, (
            f"{post_owner} is in the pool, so this case is answerable after all"
        )


def test_both_abstention_kinds_are_present(farm):
    kinds = {a.kind for a in farm.abstention_cases}
    assert kinds == {"correlation", "retrieval"}


def test_every_abstention_case_states_why(farm):
    for case in farm.abstention_cases:
        assert case.why_absent, case.case_id
        assert case.correct_behaviour == "abstain"


# --------------------------------------------------------------------------- #
# Config validation
# --------------------------------------------------------------------------- #


def test_single_service_per_cluster_is_rejected():
    """No matter would mean the correlation experiment has no positive class."""
    with pytest.raises(FarmConfigError):
        validate_config(FarmConfig(services_per_cluster=1))


def test_zero_clean_control_services_is_rejected():
    with pytest.raises(FarmConfigError):
        validate_config(FarmConfig(n_clean_control_services=0))


def test_wrong_target_flaw_count_is_rejected():
    """A mismatched count would silently corrupt the recall denominator."""
    with pytest.raises(FarmConfigError):
        validate_config(FarmConfig(target_flaw_count=99))


def test_default_config_is_valid():
    validate_config(FarmConfig())


def test_config_is_serialisable():
    payload = FarmConfig().to_dict()
    assert payload["seed"] == FarmConfig().seed
    assert payload["target_flaw_count"] == 25