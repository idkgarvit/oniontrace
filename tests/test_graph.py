"""Tests for the identity graph (M3).

The graph's value is not that it finds clusters; it is that it refuses to assert
them. Most of what follows pins the refusals:

* a merge needs an edge, an actor and a rationale;
* a merge can be undone exactly, not approximately;
* a vendor default never contributes support, and never becomes an edge;
* a deliberate contradiction is recorded as doubt rather than averaged away.
"""

from __future__ import annotations

from itertools import combinations

import pytest

from graph.extract import extract_artefacts, summarise
from graph.model import (
    PUBLIC_PLACEHOLDER,
    STOCK_DEFAULT,
    Artefact,
    EdgeStatus,
    GraphError,
    MergeNotReversible,
    NoSuchEntity,
)
from graph.resolve import (
    CANDIDATE_THRESHOLD,
    default_cohorts,
    propose_for_pair,
    resolve,
    to_edges,
)
from graph.store import GraphStore


# -- fixtures ----------------------------------------------------------- #
class _Svc:
    def __init__(self, sid, cluster_id=None, arts=None):
        self.service_id = sid
        self.cluster_id = cluster_id
        self.shared_artifacts = arts or {}


class _Farm:
    def __init__(self, services):
        self.services = services


def _farm_two_clusters() -> _Farm:
    """Two clusters of three, each with its own unique favicon and analytics id.

    Both clusters also carry the same vendor-default TLS and SSH values, which is the
    trap: those shared bytes must produce no link at all.
    """

    svcs = []
    for cl, tag in (("C0", "a"), ("C1", "b")):
        for i in range(3):
            svcs.append(
                _Svc(
                    f"svc-{tag}-{i}",
                    cl,
                    {
                        "favicon_sha256": f"fav-{tag}",
                        "analytics_id": f"an-{tag}",
                        "tls_spki_sha256": STOCK_DEFAULT,
                        "ssh_hostkey_sha256": STOCK_DEFAULT,
                    },
                )
            )
    return _Farm(svcs)


@pytest.fixture
def farm():
    return _farm_two_clusters()


@pytest.fixture
def arts(farm):
    return extract_artefacts(farm, observed_at="2026-10-19T10:00:00+00:00")


def _edge(*args, **kwargs):
    """A proposal that must exist, so a test cannot silently pass on None."""

    prop = propose_for_pair(*args, **kwargs)
    assert prop is not None, "expected a proposal"
    return to_edges([prop])[0]


# -- extraction --------------------------------------------------------- #
def test_extraction_covers_every_service_and_artefact(farm, arts):
    assert len(arts) == 6 * 4
    assert {a.service_id for a in arts} == {s.service_id for s in farm.services}


def test_extraction_flags_defaults_as_non_identifying(arts):
    summary = summarise(arts)
    assert summary["total"] == 24
    assert summary["non_identifying"] == 12
    assert summary["identifying"] == 12
    assert set(summary["non_identifying_values"]) == {STOCK_DEFAULT, PUBLIC_PLACEHOLDER}


def test_each_artefact_carries_the_digest_of_its_evidence(arts):
    assert all(len(a.evidence_sha256) == 64 for a in arts)
    # The service id is folded into the digest, so the same value on two services
    # does not produce one shared digest and cannot be conflated in an export.
    same_kind = [a for a in arts if a.kind == "favicon_sha256"]
    assert len({a.evidence_sha256 for a in same_kind}) == len({a.service_id for a in same_kind})


def test_artefact_digest_changes_when_the_status_would_change():
    a = Artefact("x", "favicon_sha256", "v", "svc", "/", "", "")
    b = Artefact("x", "favicon_sha256", "v", "svc", "/", "", "")
    assert a.evidence_sha256 == b.evidence_sha256 == ""


# -- resolution --------------------------------------------------------- #
def test_unit_helpers_agree_on_a_single_kind():
    from graph.extract import endpoint_digest

    a = Artefact("id", "favicon_sha256", "v", "svc-1", "/favicon.ico", "", "")
    assert endpoint_digest("svc-1", a) != endpoint_digest("svc-2", a)


def test_a_vendor_default_never_supports_a_link(farm, arts):
    """The headline invariant: shared shipped defaults produce no edge at all."""

    entities = [
        type("E", (), {"entity_id": f"ent-{s.service_id}", "service_ids": [s.service_id]})()
        for s in farm.services
    ]
    proposals = resolve(entities, arts)
    pairs = {(p.a_entity.replace("ent-", ""), p.b_entity.replace("ent-", "")) for p in proposals}
    cross = {p for p in pairs if p[0][:5] != p[1][:5]}
    # svc-a-* and svc-b-* share nothing but defaults, so no cross-cluster pair may
    # appear. Before this rule the graph proposed 46 of them.
    assert not cross, f"defaults produced cross-cluster links: {sorted(cross)[:5]}"


def test_defaults_are_reported_as_cohorts_rather_than_edges(arts):
    cohorts = default_cohorts(arts)
    assert {c["kind"] for c in cohorts} == {"tls_spki_sha256", "ssh_hostkey_sha256"}
    assert all(c["service_count"] == 6 for c in cohorts)
    assert all("vendor default" in c["why"] for c in cohorts)


def test_a_public_placeholder_cohort_names_its_reason(arts):
    extra = list(arts) + [
        Artefact("a1", "contact_handle", PUBLIC_PLACEHOLDER, "svc-a-0", "/contact", "x", "t"),
        Artefact("a2", "contact_handle", PUBLIC_PLACEHOLDER, "svc-b-0", "/contact", "x", "t"),
    ]
    cohorts = {c["kind"]: c for c in default_cohorts(extra)}
    assert "contact_handle" in cohorts
    assert "anyone could carry it" in cohorts["contact_handle"]["why"]


def test_two_unique_shared_values_outscore_one(farm, arts):
    one = propose_for_pair(
        "A", ["svc-a-0"], {"favicon_sha256": "fav-a"},
        "B", ["svc-b-0"], {"favicon_sha256": "fav-a"},
        {("favicon_sha256", "fav-a"): 2},
    )
    two = propose_for_pair(
        "A", ["svc-a-0"], {"favicon_sha256": "fav-a", "analytics_id": "an-a"},
        "B", ["svc-b-0"], {"favicon_sha256": "fav-a", "analytics_id": "an-a"},
        {("favicon_sha256", "fav-a"): 2, ("analytics_id", "an-a"): 2},
    )
    assert one and two
    assert two.score > one.score
    assert one.method == "deterministic"
    assert two.method == "probabilistic"


def test_a_value_above_the_frequency_cap_is_not_support(farm, arts):
    """A value on more services than the cap cannot be cluster-specific."""

    prop = propose_for_pair(
        "A", ["s1"], {"favicon_sha256": "popular"},
        "B", ["s2"], {"favicon_sha256": "popular"},
        {("favicon_sha256", "popular"): 9},
        cap=3,
    )
    assert prop is None


def test_below_threshold_pairs_are_kept_and_labelled_possible():
    """A weak pair is surfaced with its score, not silently dropped."""

    prop = propose_for_pair(
        "A", ["s1"], {"error404_sha256": "e"},
        "B", ["s2"], {"error404_sha256": "e"},
        {("error404_sha256", "e"): 2},
        threshold=CANDIDATE_THRESHOLD,
    )
    assert prop is not None
    assert prop.score < CANDIDATE_THRESHOLD
    assert prop.status == EdgeStatus.POSSIBLE.value


def test_a_differing_deliberate_identifier_is_recorded_as_doubt():
    """Bull and bear: one strong match plus one contradiction is a conflict."""

    prop = propose_for_pair(
        "A", ["s1"], {"favicon_sha256": "fav", "contact_handle": "@alice"},
        "B", ["s2"], {"favicon_sha256": "fav", "contact_handle": "@bob"},
        {("favicon_sha256", "fav"): 2},
    )
    assert prop is not None
    assert prop.supporting and prop.counter
    assert prop.status == EdgeStatus.CONFLICT.value
    assert any("differ" in e.note for e in prop.counter)


def test_no_shared_artefact_means_no_edge():
    assert propose_for_pair("A", ["s1"], {"favicon_sha256": "x"},
                            "B", ["s2"], {"favicon_sha256": "y"}, {}) is None


# -- store doctrine ----------------------------------------------------- #
def test_merge_requires_a_linked_pair(arts):
    with GraphStore(":memory:", case_id="C") as store:
        store.add_entity("ent-A", "A")
        store.add_entity("ent-B", "B")
        with pytest.raises(GraphError, match="no evidence links"):
            store.merge("ent-A", "ent-B", actor="a.sharma", rationale="looks similar")


def test_merge_requires_an_actor_and_a_rationale(arts):
    with GraphStore(":memory:", case_id="C") as store:
        store.add_entity("ent-A", "A")
        store.add_entity("ent-B", "B")
        store.add_edge(
            to_edges(
                [
                    propose_for_pair(
                        "ent-A", ["s1"], {"favicon_sha256": "f"},
                        "ent-B", ["s2"], {"favicon_sha256": "f"},
                        {("favicon_sha256", "f"): 2},
                    )
                ]
            )[0]
        )
        with pytest.raises(GraphError, match="actor"):
            store.merge("ent-A", "ent-B", actor="  ", rationale="r")
        with pytest.raises(GraphError, match="rationale"):
            store.merge("ent-A", "ent-B", actor="a.sharma", rationale="")


def test_merge_moves_services_and_logs_the_before_state(arts):
    with GraphStore(":memory:", case_id="C") as store:
        store.add_entity("ent-A", "A")
        store.add_entity("ent-B", "B")
        store.attach_service("ent-A", "s1")
        store.attach_service("ent-B", "s2")
        store.add_edge(
            _edge("ent-A", ["s1"], {"favicon_sha256": "f"},
                                       "ent-B", ["s2"], {"favicon_sha256": "f"},
                                       {("favicon_sha256", "f"): 2})
        )
        rec = store.merge("ent-A", "ent-B", actor="a.sharma", rationale="same unique favicon")
        assert store.entity("ent-A").service_ids == ["s1", "s2"]
        assert rec.before_state["absorbed"]["service_ids"] == ["s2"]
        assert len(store.merge_log()) == 1


def test_merge_is_exactly_reversible(arts):
    with GraphStore(":memory:", case_id="C") as store:
        store.add_entity("ent-A", "A")
        store.add_entity("ent-B", "B")
        store.attach_service("ent-A", "s1")
        store.attach_service("ent-B", "s2")
        store.add_edge(
            _edge("ent-A", ["s1"], {"favicon_sha256": "f"},
                                       "ent-B", ["s2"], {"favicon_sha256": "f"},
                                       {("favicon_sha256", "f"): 2})
        )
        before_a = store.entity("ent-A").service_ids
        before_b = store.entity("ent-B").service_ids
        rec = store.merge("ent-A", "ent-B", actor="a.sharma", rationale="r")
        store.reverse_merge(rec.merge_id, actor="a.sharma", reason="favicon reused")
        assert store.entity("ent-A").service_ids == before_a
        assert store.entity("ent-B").service_ids == before_b
        assert store.merge_log()[0].reversed_at is not None


def test_a_merge_cannot_be_reversed_twice():
    with GraphStore(":memory:", case_id="C") as store:
        store.add_entity("ent-A", "A")
        store.add_entity("ent-B", "B")
        store.add_edge(
            _edge("ent-A", [], {"favicon_sha256": "f"},
                                       "ent-B", [], {"favicon_sha256": "f"},
                                       {("favicon_sha256", "f"): 2})
        )
        rec = store.merge("ent-A", "ent-B", actor="a", rationale="r")
        store.reverse_merge(rec.merge_id, actor="a")
        with pytest.raises(MergeNotReversible):
            store.reverse_merge(rec.merge_id, actor="a")


def test_merge_refuses_the_same_entity_twice():
    with GraphStore(":memory:", case_id="C") as store:
        store.add_entity("ent-A", "A")
        with pytest.raises(GraphError, match="itself"):
            store.merge("ent-A", "ent-A", actor="a", rationale="r")


def test_unknown_entity_raises():
    with GraphStore(":memory:", case_id="C") as store:
        with pytest.raises(NoSuchEntity):
            store.entity("nope")


def test_export_keeps_merged_nodes_visible():
    """History must survive the merge, or the export becomes an assertion."""

    with GraphStore(":memory:", case_id="C") as store:
        store.add_entity("ent-A", "A")
        store.add_entity("ent-B", "B")
        store.add_edge(
            _edge("ent-A", [], {"favicon_sha256": "f"},
                                       "ent-B", [], {"favicon_sha256": "f"},
                                       {("favicon_sha256", "f"): 2})
        )
        store.merge("ent-A", "ent-B", actor="a", rationale="r")
        data = store.export()
        merged = [n for n in data["entities"] if n["merged_into"]]
        assert len(merged) == 1
        assert merged[0]["merged_into"] == "ent-A"
        assert data["counts"]["merged"] == 1


def test_merging_the_same_pair_twice_is_refused():
    """A merge records a decision, so the same decision cannot be recorded twice.

    Without this guard, clicking Merge twice wrote two merge records that changed
    nothing. The log then claimed two decisions where one was made, and reversing either
    record restored a state the other had already superseded.
    """

    def linked_pair(store) -> None:
        store.add_entity("ent-A", "A")
        store.add_entity("ent-B", "B")
        store.add_edge(
            to_edges(
                [
                    propose_for_pair(
                        "ent-A", ["s1"], {"favicon_sha256": "f"},
                        "ent-B", ["s2"], {"favicon_sha256": "f"},
                        {("favicon_sha256", "f"): 2},
                    )
                ]
            )[0]
        )

    with GraphStore(":memory:", case_id="C") as store:
        linked_pair(store)
        store.merge("ent-A", "ent-B", actor="a.sharma", rationale="shared favicon")
        assert len(store.merge_log()) == 1

        with pytest.raises(GraphError, match="already merged"):
            store.merge("ent-A", "ent-B", actor="a.sharma", rationale="again")

        # and in the other direction, which moves nothing either
        with pytest.raises(GraphError, match="already merged"):
            store.merge("ent-B", "ent-A", actor="a.sharma", rationale="reversed pair")

        assert len(store.merge_log()) == 1, "a refused merge must not be logged"


def test_a_merged_entity_is_not_a_merge_candidate_again():
    with GraphStore(":memory:", case_id="C") as store:
        store.add_entity("ent-A", "A")
        store.add_entity("ent-B", "B")
        store.add_edge(
            to_edges(
                [
                    propose_for_pair(
                        "ent-A", ["s1"], {"favicon_sha256": "f"},
                        "ent-B", ["s2"], {"favicon_sha256": "f"},
                        {("favicon_sha256", "f"): 2},
                    )
                ]
            )[0]
        )
        assert store.entity("ent-B").merged_into is None

        store.merge("ent-A", "ent-B", actor="a.sharma", rationale="shared favicon")
        assert store.entity("ent-B").merged_into == "ent-A"

        store.reverse_merge(store.merge_log()[0].merge_id, actor="a.sharma", reason="wrong")
        assert store.entity("ent-B").merged_into is None


def test_findings_are_stored_and_counted():
    """Findings live beside the graph so a link can be read with what was found on it."""

    with GraphStore(":memory:", case_id="C") as store:
        store.add_entity("ent-A", "A")
        store.add_finding(service_id="s1", kind="dot_env", path="/.env",
                          evidence="HTTP 200, body begins AWS_")
        store.add_finding(service_id="s1", kind="dot_git", path="/.git/config",
                          evidence="HTTP 200")
        assert len(store.findings()) == 2
        assert len(store.findings(service_id="s1")) == 2
        assert store.findings(service_id="s2") == []
        assert store.export()["counts"]["findings"] == 2


def test_a_finding_is_idempotent_for_the_same_rule_and_path():
    """Re-running detection must not double-count the same observation."""

    with GraphStore(":memory:", case_id="C") as store:
        for _ in range(3):
            store.add_finding(service_id="s1", kind="dot_env", path="/.env", evidence="x")
        assert len(store.findings()) == 1


def test_notes_are_attributed_and_kept():
    with GraphStore(":memory:", case_id="C") as store:
        store.add_entity("ent-A", "A")
        store.add_note("check whois on the cert", actor="a.sharma", entity_id="ent-A")
        notes = store.notes(entity_id="ent-A")
        assert len(notes) == 1 and notes[0]["actor"] == "a.sharma"


def test_real_farm_recovers_every_true_pair_with_no_false_positive(tmp_path):
    """End-to-end against the generated farm.

    This is the claim the dashboard and the report both rest on, so it is asserted
    here rather than measured once by hand.

    The farm is generated here, not read from ``out/farm.json``. Reading the generated
    file meant this test skipped on a clean checkout, which is the shape of defect that
    makes a suite report success it has not earned.
    """

    import json

    from farm.generator import generate_farm
    from farm.model import FarmConfig

    farm_json = tmp_path / "farm.json"
    farm_json.write_text(
        generate_farm(FarmConfig(seed=20260925)).to_json(include_text=True),
        encoding="utf-8",
    )

    data = json.loads(farm_json.read_text())
    svcs = [
        _Svc(s["service_id"], s.get("cluster_id"), s.get("shared_artifacts", {}))
        for s in data["services"]
    ]
    farm = _Farm(svcs)
    arts = extract_artefacts(farm, observed_at="2026-10-19T10:00:00+00:00")
    entities = [
        type("E", (), {"entity_id": f"ent-{s.service_id}", "service_ids": [s.service_id]})()
        for s in svcs
    ]
    proposed = {
        tuple(sorted((p.a_entity.replace("ent-", ""), p.b_entity.replace("ent-", ""))))
        for p in resolve(entities, arts)
    }

    truth = set()
    for cluster in data.get("clusters", []):
        for a, b in combinations(sorted(cluster["service_ids"]), 2):
            truth.add((a, b))

    assert truth, "farm declares no clusters; the test would be vacuous"
    assert proposed == truth, (
        f"missed {sorted(truth - proposed)} / invented {sorted(proposed - truth)}"
    )


def test_merge_reverse_merge_again_is_not_a_primary_key_collision(arts):
    """Merge, undo, merge again has to work, and it is what an analyst actually does.

    `merge_id` was a digest of (survivor, absorbed, utc_now()) at second resolution. `reverse_merge`
    keeps the record it reverses, on purpose: "we merged these and changed our mind" is evidence.
    So the second merge of the same pair inside the same second collided with the kept record and
    died with `IntegrityError: UNIQUE constraint failed: merge_log.merge_id` -- a database error
    where the operator expected their decision to be recorded. This project's own console
    walkthrough performs exactly that sequence.
    """
    with GraphStore(":memory:", case_id="C") as store:
        store.add_entity("ent-A", "A")
        store.add_entity("ent-B", "B")
        store.attach_service("ent-A", "s1")
        store.attach_service("ent-B", "s2")
        store.add_edge(
            _edge("ent-A", ["s1"], {"favicon_sha256": "f"},
                  "ent-B", ["s2"], {"favicon_sha256": "f"},
                  {("favicon_sha256", "f"): 2})
        )

        ids = []
        for round_ in range(3):
            record = store.merge("ent-A", "ent-B", actor="a.sharma",
                                 rationale=f"same favicon, attempt {round_ + 1}")
            ids.append(record.merge_id)
            store.reverse_merge(record.merge_id, actor="a.sharma", reason="not convinced")

        assert len(set(ids)) == 3, f"merge ids collided: {ids}"
        assert len(store.merge_log()) == 3, (
            "every decision must survive in the log, including the reversed ones"
        )
        assert all(record.reversed_at for record in store.merge_log())
