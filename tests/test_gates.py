"""Tests for the Part E2 deployment gates.

Each test corresponds to a requirement in the plan, and the names are deliberately
requirement-shaped: if one fails, the answer to "does the tool do what the report says?"
has changed. A gate is only worth having if its refusals are exercised, so most of these
assert that something was *refused* -- and that the refusal is on the record.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from gates.acceptable_use import STATEMENT_PATH, statement_text
from gates.audit import AuditAction, AuditLog
from gates.cases import LinkDecision
from gates.errors import (
    AuditTampered,
    CaseError,
    GateRefused,
    IdentityAssertion,
    PermissionDenied,
    PresentationError,
)
from gates.exports import Watermark, build_evidence_pack
from gates.gate import PERMIT_TTL_MINUTES, CollectionGate
from gates.presentation import (
    BANNED_PATTERNS,
    STATE_LABEL,
    CandidateLink,
    LinkState,
    assert_safe,
    lint,
    render_candidate,
    state_for,
)
from gates.roles import ROLE_RANK, Permission, Principal, Role, may

CASE = "CASE-2026-014"

VIEWER = Principal(principal_id="v.nair", role=Role.VIEWER)
ANALYST = Principal(principal_id="a.sharma", role=Role.ANALYST)
OWNER = Principal(principal_id="a.sharma", role=Role.CASE_OWNER)
OTHER_OWNER = Principal(principal_id="b.rao", role=Role.CASE_OWNER)
ADMIN = Principal(principal_id="r.iyer", role=Role.ADMIN)


class FakeClock:
    """A clock the tests move by hand, so retention can be outlived in milliseconds."""

    def __init__(self, start: str = "2026-09-28T00:00:00+00:00") -> None:
        self.now = datetime.fromisoformat(start)

    def __call__(self) -> str:
        return self.now.isoformat(timespec="seconds")

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def gate(tmp_path, clock) -> CollectionGate:
    return CollectionGate(tmp_path / "gate", clock=clock)


@pytest.fixture
def acked(gate) -> CollectionGate:
    """A gate whose deployment has acknowledged the acceptable-use statement."""
    gate.acknowledge(OWNER)
    return gate


@pytest.fixture
def open_case(acked) -> str:
    acked.open_case(
        OWNER,
        case_id=CASE,
        title="Escrow forum operator correlation",
        authorisation_ref="AUTH-2026-0091",
        retention_days=30,
    )
    return CASE


def link(score: float = 0.41, **overrides) -> CandidateLink:
    payload = {
        "case_id": CASE,
        "left": "svc-c0-0",
        "right": "svc-c0-3",
        "score": score,
        "method": "shared favicon hash",
        "evidence_refs": ("sha256:aaaa", "snapshot:snapshot-0000.json"),
        "counter_evidence": (),
    }
    payload.update(overrides)
    return CandidateLink(**payload)


# --------------------------------------------------------------------------- #
# E2.2 Case scoping, the authorisation gate, retention and purge
# --------------------------------------------------------------------------- #


def test_collection_is_refused_without_a_case(acked):
    """No case, no collection -- the requirement this whole package exists for."""
    with pytest.raises(GateRefused) as caught:
        acked.authorise_collection("CASE-DOES-NOT-EXIST", OWNER)
    assert "E2.2" in str(caught.value)


def test_a_case_needs_an_authorisation_reference(acked):
    """Blank authority is not authority, so it is refused rather than defaulted."""
    with pytest.raises(CaseError) as caught:
        acked.open_case(
            OWNER,
            case_id=CASE,
            title="no authority recorded",
            authorisation_ref="   ",
            retention_days=30,
        )
    assert "authorisation" in str(caught.value)


def test_a_case_needs_a_named_owner(acked):
    """Even an admin cannot open a case with no owning analyst.

    Tested as an admin because only an admin may supply an owner at all -- a non-admin's
    case is always owned by themselves, so this check is reachable from the admin path
    and would otherwise be dead code that looks like validation.
    """
    with pytest.raises(CaseError) as caught:
        acked.open_case(
            ADMIN,
            case_id=CASE,
            title="ownerless",
            authorisation_ref="AUTH-1",
            retention_days=30,
            owner="   ",
        )
    assert "owning analyst" in str(caught.value)


def test_unlimited_retention_is_refused(acked):
    """An unlimited case is a case nobody decided to end."""
    with pytest.raises(CaseError):
        acked.open_case(
            OWNER,
            case_id=CASE,
            title="forever",
            authorisation_ref="AUTH-1",
            retention_days=0,
        )


def test_retention_expiry_is_set_when_the_case_is_opened(acked):
    case = acked.open_case(
        OWNER,
        case_id=CASE,
        title="thirty days",
        authorisation_ref="AUTH-1",
        retention_days=30,
    )
    assert case.retention_until == "2026-10-28T00:00:00+00:00"


def test_seeds_belong_to_one_case_and_are_not_global(acked):
    """There is no global crawl list: a seed in one case is invisible to another."""
    acked.open_case(
        OWNER, case_id="CASE-A", title="A", authorisation_ref="AUTH-A", retention_days=30
    )
    acked.open_case(
        OWNER, case_id="CASE-B", title="B", authorisation_ref="AUTH-B", retention_days=30
    )
    acked.add_seed("CASE-A", "http://alpha.onion", OWNER)

    assert acked.cases.get("CASE-A").seeds == ["http://alpha.onion"]
    assert acked.cases.get("CASE-B").seeds == []


def test_duplicate_seeds_are_not_stored_twice(acked, open_case):
    acked.add_seed(open_case, "http://alpha.onion", OWNER)
    case = acked.add_seed(open_case, "http://alpha.onion", OWNER)
    assert case.seeds == ["http://alpha.onion"]


def test_collection_is_refused_once_retention_has_expired(acked, clock):
    acked.open_case(
        OWNER, case_id=CASE, title="short lived", authorisation_ref="AUTH-1", retention_days=1
    )
    clock.advance(days=2)
    with pytest.raises(GateRefused) as caught:
        acked.authorise_collection(CASE, OWNER)
    assert "retention expired" in str(caught.value)


def test_collection_is_refused_into_a_sealed_case(acked, open_case):
    acked.cases.seal(open_case)
    with pytest.raises(CaseError) as caught:
        acked.authorise_collection(open_case, OWNER)
    assert "sealed" in str(caught.value)


def test_collection_is_refused_into_a_purged_case(acked, open_case):
    acked.purge(open_case, ADMIN)
    with pytest.raises(CaseError) as caught:
        acked.authorise_collection(open_case, OWNER)
    assert "purged" in str(caught.value)


def test_purge_removes_snapshots_and_keeps_the_tombstone(acked, open_case):
    """The authorisation record is the only evidence left once the bytes are gone."""
    permit = acked.authorise_collection(open_case, OWNER)
    acked.ingest(permit, OWNER, [{"body": "one"}, {"body": "two"}])
    assert len(acked.cases.snapshot_names(open_case)) == 2

    purged = acked.purge(open_case, ADMIN)

    assert acked.cases.snapshot_names(open_case) == ()
    assert not acked.cases.snapshots_dir(open_case).exists()
    assert purged.purged is True
    assert purged.purged_snapshots == 2
    assert purged.authorisation_ref == "AUTH-2026-0091"
    assert purged.owner == "a.sharma"
    assert purged.seeds == []


def test_purge_leaves_the_audit_trail_intact(acked, open_case):
    """Case data and the record of the case live in separate trees on purpose."""
    permit = acked.authorise_collection(open_case, OWNER)
    acked.ingest(permit, OWNER, [{"body": "one"}])
    before = len(acked.audit.for_case(open_case))

    acked.purge(open_case, ADMIN)

    assert len(acked.audit.for_case(open_case)) > before
    assert acked.audit.path.exists()
    assert acked.audit.verify()["verified"] is True


def test_purge_deletes_analyst_decisions_as_derived_data(acked, open_case):
    acked.confirm_link(open_case, link(0.97), OWNER)
    assert acked.cases.decision_for(open_case, "svc-c0-0", "svc-c0-3") is not None

    acked.purge(open_case, ADMIN)

    assert acked.cases.decision_for(open_case, "svc-c0-0", "svc-c0-3") is None


def test_sweep_expired_purges_only_the_cases_that_expired(acked, clock):
    acked.open_case(
        OWNER, case_id="CASE-SHORT", title="s", authorisation_ref="A", retention_days=1
    )
    acked.open_case(
        OWNER, case_id="CASE-LONG", title="l", authorisation_ref="B", retention_days=90
    )
    for case_id in ("CASE-SHORT", "CASE-LONG"):
        permit = acked.authorise_collection(case_id, OWNER)
        acked.ingest(permit, OWNER, [{"body": "x"}])

    clock.advance(days=10)
    swept = acked.sweep_expired(ADMIN)

    assert swept == ("CASE-SHORT",)
    assert acked.cases.get("CASE-SHORT").purged is True
    assert acked.cases.get("CASE-LONG").purged is False
    assert len(acked.cases.snapshot_names("CASE-LONG")) == 1


def test_sweep_expired_records_why_each_case_was_purged(acked, clock):
    acked.open_case(
        OWNER, case_id=CASE, title="t", authorisation_ref="A", retention_days=1
    )
    clock.advance(days=5)
    acked.sweep_expired(ADMIN)

    purges = [e for e in acked.audit.for_case(CASE) if e.action == AuditAction.PURGE.value]
    assert purges and purges[-1].detail["trigger"] == "retention_expiry"


# --------------------------------------------------------------------------- #
# Permits: the mechanism behind "no case, no collection"
# --------------------------------------------------------------------------- #


def test_a_valid_permit_allows_exactly_one_collection_run(acked, open_case):
    permit = acked.authorise_collection(open_case, OWNER)
    names = acked.ingest(permit, OWNER, [{"body": "one"}, {"body": "two"}])
    assert names == ("snapshot-0000.json", "snapshot-0001.json")


def test_a_forged_permit_is_refused(acked, open_case):
    """A permit that was not issued by the gate is not a permit."""
    permit = acked.authorise_collection(open_case, OWNER)
    forged = type(permit)(**{**permit.to_dict(), "signature": "0" * 64})
    with pytest.raises(GateRefused) as caught:
        acked.ingest(forged, OWNER, [{"body": "x"}])
    assert "signature does not verify" in str(caught.value)


def test_a_permit_cannot_be_edited_to_point_at_another_case(acked, open_case):
    """Moving a permit to another case breaks the signature that bound it."""
    acked.open_case(
        OWNER, case_id="CASE-B", title="B", authorisation_ref="B", retention_days=30
    )
    permit = acked.authorise_collection(open_case, OWNER)
    moved = type(permit)(**{**permit.to_dict(), "case_id": "CASE-B"})
    with pytest.raises(GateRefused) as caught:
        acked.ingest(moved, OWNER, [{"body": "x"}])
    assert "signature does not verify" in str(caught.value)
    assert acked.cases.snapshot_names("CASE-B") == ()


def test_a_permit_cannot_be_presented_by_another_principal(acked, open_case):
    permit = acked.authorise_collection(open_case, OWNER)
    with pytest.raises(GateRefused) as caught:
        acked.ingest(permit, ADMIN, [{"body": "x"}])
    assert "being presented by" in str(caught.value)


def test_an_expired_permit_is_refused(acked, open_case, clock):
    permit = acked.authorise_collection(open_case, OWNER)
    clock.advance(minutes=PERMIT_TTL_MINUTES + 1)
    with pytest.raises(GateRefused) as caught:
        acked.ingest(permit, OWNER, [{"body": "x"}])
    assert "expired" in str(caught.value)
    assert acked.cases.snapshot_names(open_case) == ()


def test_a_permit_stops_working_if_the_case_is_purged_before_use(acked, open_case):
    """A permit is not a licence to write into a case that has since been closed."""
    permit = acked.authorise_collection(open_case, OWNER)
    acked.purge(open_case, ADMIN)
    with pytest.raises(CaseError):
        acked.ingest(permit, OWNER, [{"body": "x"}])


# --------------------------------------------------------------------------- #
# E2.3 Audit
# --------------------------------------------------------------------------- #


def test_audit_records_the_whole_lifecycle_of_a_case(acked, open_case):
    """Every action E2.3 names appears on the trail."""
    acked.add_seed(open_case, "http://alpha.onion", OWNER)
    permit = acked.authorise_collection(open_case, OWNER)
    acked.ingest(permit, OWNER, [{"body": "x"}])
    acked.record_query(open_case, "shared favicon hash", OWNER)
    acked.confirm_link(open_case, link(0.97), OWNER)
    acked.export_evidence_pack(open_case, OWNER, [link(0.97)], snapshot_count=1)
    acked.purge(open_case, ADMIN)

    actions = [e.action for e in acked.audit.for_case(open_case)]
    for expected in (
        AuditAction.CASE_OPENED,
        AuditAction.SEED_ADDED,
        AuditAction.COLLECTION_RUN,
        AuditAction.SNAPSHOT_STORED,
        AuditAction.QUERY,
        AuditAction.LINK_CONFIRMED,
        AuditAction.EXPORT,
        AuditAction.PURGE,
    ):
        assert expected.value in actions, expected


def test_audit_records_every_named_analyst_action(acked, open_case):
    candidate = link(0.97)
    acked.confirm_link(open_case, candidate, OWNER)
    acked.reject_link(open_case, link(0.2), OWNER, reason="counter-evidence is decisive")
    acked.annotate_link(open_case, candidate, "same favicon as last year's set", OWNER)
    acked.reverse_link_action(
        open_case, candidate, OWNER, reason="favicon rotated; evidence withdrawn"
    )

    actions = [e.action for e in acked.audit.for_case(open_case)]
    assert AuditAction.LINK_CONFIRMED.value in actions
    assert AuditAction.LINK_REJECTED.value in actions
    assert AuditAction.LINK_ANNOTATED.value in actions
    assert AuditAction.LINK_ACTION_REVERSED.value in actions


def test_audit_is_append_only_and_detects_an_edited_entry(acked, open_case):
    acked.record_query(open_case, "first query", OWNER)
    acked.record_query(open_case, "second query", OWNER)
    assert acked.audit.verify()["verified"] is True

    lines = acked.audit.path.read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[0])
    entry["actor"] = "somebody.else"
    lines[0] = json.dumps(entry, sort_keys=True)
    acked.audit.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = acked.audit.verify()
    assert result["verified"] is False
    assert result["first_bad_seq"] == 0
    with pytest.raises(AuditTampered):
        acked.audit.assert_untampered()


def test_audit_detects_a_removed_entry(acked, open_case):
    acked.record_query(open_case, "first", OWNER)
    acked.record_query(open_case, "second", OWNER)
    acked.record_query(open_case, "third", OWNER)

    lines = acked.audit.path.read_text(encoding="utf-8").splitlines()
    del lines[1]  # remove the middle entry, as an operator covering a step would
    acked.audit.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert acked.audit.verify()["verified"] is False


def test_audit_detects_reordered_entries(acked, open_case):
    acked.record_query(open_case, "first", OWNER)
    acked.record_query(open_case, "second", OWNER)

    lines = acked.audit.path.read_text(encoding="utf-8").splitlines()
    acked.audit.path.write_text("\n".join(reversed(lines)) + "\n", encoding="utf-8")

    assert acked.audit.verify()["verified"] is False


def test_the_audit_offers_no_public_write_method():
    """An audit trail an actor can write to is testimony, not evidence."""
    public = {
        name
        for name in dir(AuditLog)
        if not name.startswith("_") and callable(getattr(AuditLog, name))
    }
    assert public == {"entries", "for_case", "read", "verify", "assert_untampered"}


def test_an_analyst_cannot_read_the_audit_trail(acked, open_case):
    """Reading the trail is itself a disclosure, so it carries its own permission."""
    with pytest.raises(PermissionDenied):
        acked.audit.read(OWNER, case_id=open_case)
    assert acked.audit.read(ADMIN, case_id=open_case)


def test_reversal_requires_a_reason(acked, open_case):
    """An unexplained reversal cannot be told apart from a mistake."""
    candidate = link(0.97)
    acked.confirm_link(open_case, candidate, OWNER)
    with pytest.raises(GateRefused) as caught:
        acked.reverse_link_action(open_case, candidate, OWNER, reason="   ")
    assert "must carry a reason" in str(caught.value)
    # The decision is still in place: a refused reversal must not half-apply.
    assert acked.cases.decision_for(open_case, "svc-c0-0", "svc-c0-3") is not None


def test_reversal_restores_the_unconfirmed_state(acked, open_case):
    candidate = link(0.97)
    acked.confirm_link(open_case, candidate, OWNER)
    assert acked.cases.decision_for(open_case, "svc-c0-0", "svc-c0-3").action == "confirmed"

    acked.reverse_link_action(
        open_case, candidate, OWNER, reason="evidence withdrawn by the source team"
    )

    assert acked.cases.decision_for(open_case, "svc-c0-0", "svc-c0-3") is None
    rendered = acked.view_candidates(OWNER, open_case, [candidate])[0]
    assert "confirmed by" not in rendered


def test_reversal_is_logged_with_what_it_undid(acked, open_case):
    candidate = link(0.97)
    acked.confirm_link(open_case, candidate, OWNER)
    original_seq = acked.cases.decision_for(open_case, "svc-c0-0", "svc-c0-3").audit_seq
    acked.reverse_link_action(open_case, candidate, OWNER, reason="withdrawn")

    entry = [
        e
        for e in acked.audit.for_case(open_case)
        if e.action == AuditAction.LINK_ACTION_REVERSED.value
    ][-1]
    assert entry.detail["reversed_action"] == "confirmed"
    assert entry.detail["reversed_decided_by"] == "a.sharma"
    assert entry.detail["reversed_audit_seq"] == original_seq
    assert entry.detail["reason"] == "withdrawn"


def test_refusals_are_recorded_on_the_trail(acked, open_case):
    """A trail that logs only successes cannot answer "did anyone try?"."""
    with pytest.raises(PermissionDenied):
        acked.purge(open_case, ANALYST)

    refusals = [
        e
        for e in acked.audit.for_case(open_case)
        if e.action == AuditAction.GATE_REFUSED.value
    ]
    assert refusals, "the refused purge was not recorded"
    assert "purge" in refusals[-1].detail["error"]


def test_decisions_survive_a_new_gate_instance(tmp_path, clock):
    """Decisions are persisted, so a restart does not silently un-decide anything."""
    first = CollectionGate(tmp_path / "gate", clock=clock)
    first.acknowledge(OWNER)
    first.open_case(
        OWNER, case_id=CASE, title="t", authorisation_ref="A", retention_days=30
    )
    first.confirm_link(CASE, link(0.97), OWNER)

    second = CollectionGate(tmp_path / "gate", clock=clock)
    decision = second.cases.decision_for(CASE, "svc-c0-0", "svc-c0-3")
    assert isinstance(decision, LinkDecision)
    assert decision.action == "confirmed"
    assert second.audit.verify()["verified"] is True


def test_a_link_and_its_reverse_share_one_decision(acked, open_case):
    acked.confirm_link(open_case, link(0.97), OWNER)
    same_pair = acked.cases.decision_for(open_case, "svc-c0-3", "svc-c0-0")
    assert same_pair is not None and same_pair.action == "confirmed"


# --------------------------------------------------------------------------- #
# E2.4 Access control
# --------------------------------------------------------------------------- #


def test_candidate_links_require_analyst_or_above(acked, open_case):
    with pytest.raises(PermissionDenied) as caught:
        acked.view_candidates(VIEWER, open_case, [link(0.97)])
    assert "view_candidate_links" in str(caught.value)
    assert "analyst" in str(caught.value)
    # Analyst and owner can both see them.
    assert acked.view_candidates(ANALYST, open_case, [link(0.97)])
    assert acked.view_candidates(OWNER, open_case, [link(0.97)])


def test_export_requires_case_owner(acked, open_case):
    with pytest.raises(PermissionDenied) as caught:
        acked.export_evidence_pack(open_case, ANALYST, [link(0.97)])
    assert "export" in str(caught.value)
    assert acked.export_evidence_pack(open_case, OWNER, [link(0.97)])


def test_purge_requires_admin(acked, open_case):
    for principal in (VIEWER, ANALYST, OWNER):
        with pytest.raises(PermissionDenied):
            acked.purge(open_case, principal)
    assert acked.purge(open_case, ADMIN).purged is True


def test_case_owner_role_does_not_confer_authority_over_someone_elses_case(acked, open_case):
    """Otherwise "case owner" would just mean "trusted person"."""
    with pytest.raises(PermissionDenied) as caught:
        acked.export_evidence_pack(open_case, OTHER_OWNER, [link(0.97)])
    assert "case ownership" in str(caught.value)
    with pytest.raises(PermissionDenied):
        acked.add_seed(open_case, "http://beta.onion", OTHER_OWNER)
    with pytest.raises(PermissionDenied):
        acked.authorise_collection(open_case, OTHER_OWNER)


def test_an_admin_may_act_on_a_case_they_do_not_own(acked, open_case):
    """Someone has to be able to resolve a dispute."""
    permit = acked.authorise_collection(open_case, ADMIN)
    assert acked.ingest(permit, ADMIN, [{"body": "x"}])
    assert acked.export_evidence_pack(open_case, ADMIN, [link(0.97)])


def test_a_role_may_not_open_a_case_in_another_analysts_name(acked):
    with pytest.raises(PermissionDenied):
        acked.open_case(
            OWNER,
            case_id=CASE,
            title="borrowed authority",
            authorisation_ref="AUTH-1",
            retention_days=30,
            owner="b.rao",
        )


def test_roles_are_ordered_so_admin_implies_every_lower_permission():
    assert ROLE_RANK[Role.ADMIN] > ROLE_RANK[Role.CASE_OWNER]
    assert ROLE_RANK[Role.CASE_OWNER] > ROLE_RANK[Role.ANALYST]
    assert ROLE_RANK[Role.ANALYST] > ROLE_RANK[Role.VIEWER]
    for permission in Permission:
        assert may(ADMIN, permission), permission


def test_a_principal_needs_an_identifier_to_be_auditable():
    with pytest.raises(ValueError):
        Principal(principal_id="   ", role=Role.ANALYST)


# --------------------------------------------------------------------------- #
# E2.5 Presentation rules
# --------------------------------------------------------------------------- #


def test_rendered_output_never_asserts_identity(acked, open_case):
    """The strongest permitted string is a candidate link with score and evidence."""
    rendered = acked.view_candidates(OWNER, open_case, [link(0.41), link(0.97)])[0]
    assert lint(rendered) == []
    assert "candidate link" in rendered.lower()


@pytest.mark.parametrize(
    "text",
    [
        "these two addresses match",
        "the favicon matched",
        "this is the same person",
        "the same operator runs both",
        "the operator was identified",
        "we identify the author",
    ],
)
def test_the_lint_catches_each_banned_family(text):
    assert lint(text), f"lint missed {text!r}"
    with pytest.raises(IdentityAssertion):
        assert_safe(text)


@pytest.mark.parametrize(
    "text",
    [
        "shared favicon hash",
        "identical robots.txt",
        "artifact overlap: pgp_fingerprint",
        "identity graph",
        "unconfirmed",
    ],
)
def test_the_lint_does_not_fire_on_permitted_wording(text):
    """A lint that fires on legitimate wording gets switched off, so it must be precise."""
    assert lint(text) == [], f"{text!r} was flagged as an assertion"


def test_a_detector_named_with_a_banned_word_fails_to_render(acked, open_case):
    """If a method is called "favicon match", rendering it fails -- and that is the fix."""
    bad = link(0.97, method="favicon match")
    with pytest.raises(IdentityAssertion):
        acked.view_candidates(OWNER, open_case, [bad])


def test_below_the_threshold_renders_as_possible(acked, open_case):
    assert state_for(0.41, confirm_threshold=0.95) is LinkState.POSSIBLE
    rendered = acked.view_candidates(OWNER, open_case, [link(0.41)])[0]
    assert "possible" in rendered
    assert "unconfirmed" in rendered


def test_clearing_the_threshold_is_still_unconfirmed(acked, open_case):
    """A score bar is not a human judgement."""
    assert state_for(0.97, confirm_threshold=0.95) is LinkState.UNCONFIRMED
    rendered = acked.view_candidates(OWNER, open_case, [link(0.97)])[0]
    assert "unconfirmed" in rendered
    assert "confirmed by" not in rendered


def test_only_an_analyst_decision_produces_the_confirmed_state(acked, open_case):
    candidate = link(0.97)
    acked.confirm_link(open_case, candidate, OWNER)
    rendered = acked.view_candidates(OWNER, open_case, [candidate])[0]
    assert "confirmed by analyst decision" in rendered


def test_counter_evidence_is_shown_when_it_exists():
    """Curating it away would present a conclusion the evidence does not support."""
    rendered = render_candidate(
        link(0.41, counter_evidence=("distinct TLS certificate fingerprint",)),
        confirm_threshold=0.95,
    )
    assert "counter-evidence: distinct TLS certificate fingerprint" in rendered


def test_absent_counter_evidence_is_stated_rather_than_omitted():
    """Silence is ambiguous between "there is none" and "nobody looked"."""
    rendered = render_candidate(link(0.41), confirm_threshold=0.95)
    assert "counter-evidence: none recorded" in rendered


def test_a_candidate_without_evidence_references_is_refused():
    with pytest.raises(PresentationError):
        CandidateLink(
            case_id=CASE,
            left="a",
            right="b",
            score=0.9,
            method="shared favicon hash",
            evidence_refs=(),
        )


def test_the_abstention_state_exists_and_is_reachable(acked, open_case):
    rendered = acked.render_links_or_abstain(OWNER, open_case, ())[0]
    assert "no candidate link above the confirm threshold" in rendered
    assert "abstained" in rendered
    assert lint(rendered) == []


def test_the_abstention_state_is_permission_gated_like_a_candidate(acked, open_case):
    """No route lets a principal who may not see candidates learn that there are none."""
    with pytest.raises(PermissionDenied):
        acked.render_links_or_abstain(VIEWER, open_case, ())


def test_every_declared_state_has_a_label():
    for state in LinkState:
        assert state in STATE_LABEL


def test_every_banned_family_is_documented():
    names = {name for name, _ in BANNED_PATTERNS}
    assert names == {"match", "same person", "is the same", "identified"}


# --------------------------------------------------------------------------- #
# E2.4 Export watermarking
# --------------------------------------------------------------------------- #


def test_an_export_carries_case_analyst_timestamp_and_reference(acked, open_case):
    pack = acked.export_evidence_pack(open_case, OWNER, [link(0.97)], snapshot_count=3)
    assert "case=" + CASE in pack
    assert "analyst=a.sharma" in pack
    assert "generated_at=2026-09-28T00:00:00+00:00" in pack
    assert "retrieval_reference=ONION-" + CASE in pack


def test_the_watermark_appears_at_both_ends_of_the_pack(acked, open_case):
    """A page separated from its header still identifies where it came from."""
    pack = acked.export_evidence_pack(open_case, OWNER, [link(0.97)])
    lines = [line for line in pack.splitlines() if line.startswith("WATERMARK")]
    assert len(lines) == 2


def test_every_export_is_recorded_with_its_reference(acked, open_case):
    pack = acked.export_evidence_pack(open_case, OWNER, [link(0.97)])
    reference = [
        line.split("retrieval_reference=")[1]
        for line in pack.splitlines()
        if line.startswith("WATERMARK")
    ][0]

    exports = [
        e for e in acked.audit.for_case(open_case) if e.action == AuditAction.EXPORT.value
    ]
    assert exports
    assert exports[-1].detail["retrieval_reference"] == reference


def test_the_pack_records_analyst_decisions_and_who_made_them(acked, open_case):
    candidate = link(0.97)
    acked.confirm_link(open_case, candidate, OWNER)
    pack = acked.export_evidence_pack(open_case, OWNER, [candidate])
    assert "recorded: confirmed by a.sharma" in pack


def test_the_pack_carries_the_governing_statement_and_the_authorisation(acked, open_case):
    pack = acked.export_evidence_pack(open_case, OWNER, [link(0.97)])
    assert "ACCEPTABLE_USE.md" in pack
    assert "AUTH-2026-0091" in pack


def test_an_empty_export_renders_the_abstention_state_rather_than_nothing(acked, open_case):
    pack = acked.export_evidence_pack(open_case, OWNER, [])
    assert "no candidate link above the confirm threshold" in pack


def test_a_pack_that_would_assert_identity_is_refused(acked, open_case):
    """The export path goes through the same lint as the screen, so there is no bypass."""
    with pytest.raises(IdentityAssertion):
        acked.export_evidence_pack(open_case, OWNER, [link(0.97, method="favicon match")])


def test_a_refused_export_leaves_no_export_on_the_record(acked, open_case):
    """The pack is built before the export is logged, so a failure leaves no trace of one."""
    with pytest.raises(IdentityAssertion):
        acked.export_evidence_pack(open_case, OWNER, [link(0.97, method="favicon match")])
    exports = [
        e for e in acked.audit.for_case(open_case) if e.action == AuditAction.EXPORT.value
    ]
    assert exports == []


def test_the_pack_builder_is_usable_without_a_gate():
    """The M6-lite PDF writer (WP2.6) will call this directly."""
    pack = _pack_with()
    assert "common author" in pack
    assert lint(pack) == []


def _pack_with(decisions=()):
    from gates.cases import Case

    case = Case(
        case_id=CASE,
        title="t",
        owner="a.sharma",
        authorisation_ref="AUTH-1",
        opened_at="2026-09-28T00:00:00+00:00",
        retention_until="2026-10-28T00:00:00+00:00",
    )
    return build_evidence_pack(
        case=case,
        watermark=Watermark(
            case_id=CASE,
            analyst_id="a.sharma",
            generated_at="2026-09-28T00:00:00+00:00",
            retrieval_reference="ONION-TEST-0000000000",
        ),
        decisions=decisions,
        confirm_threshold=0.95,
        snapshot_count=0,
    )


# --------------------------------------------------------------------------- #
# E2.6 Acceptable use
# --------------------------------------------------------------------------- #


def test_the_acceptable_use_statement_is_shipped():
    assert STATEMENT_PATH.exists()
    text = statement_text()
    assert len(text.splitlines()) < 90, "the statement is meant to be about a page"


def _statement_flat() -> str:
    """The statement with line wrapping removed, so assertions test wording not layout."""
    return " ".join(statement_text().split())


def test_the_statement_denies_that_collection_creates_authority():
    """The load-bearing sentence from E2.1, in the shipped document."""
    assert (
        "does not assert that collection alone creates lawful authority"
        in _statement_flat()
    ), "the statement has lost the sentence that protects the project and the operator"


def test_the_statement_lists_the_conditions_of_use():
    text = _statement_flat().lower()
    for condition in ("lawful authority", "journalists", "no redistribution", "retention"):
        assert condition in text, condition


def test_a_case_cannot_be_opened_before_the_statement_is_acknowledged(gate):
    with pytest.raises(GateRefused) as caught:
        gate.open_case(
            OWNER,
            case_id=CASE,
            title="premature",
            authorisation_ref="AUTH-1",
            retention_days=30,
        )
    assert "acknowledged" in str(caught.value)


def test_collection_is_refused_before_the_statement_is_acknowledged(gate):
    with pytest.raises(GateRefused) as caught:
        gate.authorise_collection(CASE, OWNER)
    assert "E2.6" in str(caught.value)


def test_the_acknowledgement_names_the_actor_and_the_version(gate):
    record = gate.acknowledge(OWNER)
    assert record.principal_id == "a.sharma"
    assert record.role == "case_owner"
    assert len(record.statement_digest) == 16
    assert gate.acknowledgements.is_acknowledged() is True


def test_the_acknowledgement_is_recorded_on_the_trail(gate):
    gate.acknowledge(OWNER)
    actions = [e.action for e in gate.audit.entries()]
    assert AuditAction.ACKNOWLEDGEMENT_RECORDED.value in actions


def test_the_acknowledgement_is_per_deployment_not_per_case(acked):
    """A prompt before every run is a click; by the third time it is not read."""
    acked.open_case(
        OWNER, case_id="CASE-1", title="a", authorisation_ref="A", retention_days=30
    )
    acked.open_case(
        OWNER, case_id="CASE-2", title="b", authorisation_ref="B", retention_days=30
    )
    assert (
        len(
            [
                e
                for e in acked.audit.entries()
                if e.action == AuditAction.ACKNOWLEDGEMENT_RECORDED.value
            ]
        )
        == 1
    )


def test_purge_works_even_without_acknowledgement(gate):
    """Refusing to purge would mean refusing to comply with retention."""
    case = gate.cases.create(
        case_id="CASE-UNACK",
        title="opened outside the gate",
        owner="a.sharma",
        authorisation_ref="AUTH-1",
        retention_days=1,
    )
    gate.cases.store_snapshot(case.case_id, {"body": "x"})
    assert gate.purge(case.case_id, ADMIN).purged is True


# --------------------------------------------------------------------------- #
# E2.7 Architectural non-capabilities
# --------------------------------------------------------------------------- #


def test_the_gates_package_has_no_network_or_subprocess_capability():
    """E2.7 says the code paths do not exist -- so this asserts that they do not.

    "We would not do that" is a policy. "The gate cannot do that" is a property, and this
    is the test that keeps it one as the package grows.
    """
    import ast
    from pathlib import Path

    forbidden = {
        "socket",
        "ssl",
        "requests",
        "urllib",
        "urllib3",
        "http",
        "httpx",
        "aiohttp",
        "subprocess",
        "telnetlib",
        "ftplib",
        "smtplib",
        "paramiko",
    }
    package = Path(__file__).resolve().parent.parent / "gates"
    sources = sorted(package.glob("*.py"))
    assert sources, f"no sources found under {package}; this test would pass vacuously"
    offenders: list[str] = []
    for path in sources:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in forbidden:
                        offenders.append(f"{path.name}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module.split(".")[0] in forbidden:
                    offenders.append(f"{path.name}: from {node.module} import ...")
    assert offenders == [], f"the gate package can reach the network: {offenders}"


def test_the_cli_demo_runs_and_refuses_cleanly(tmp_path, capsys):
    """The demo is the artefact a marker or reviewer actually runs."""
    from gates.cli import main

    assert main(["--root", str(tmp_path / "casework"), "demo"]) == 0
    output = capsys.readouterr().out
    assert "REFUSED  collect with no case at all" in output
    assert "REFUSED  purge as analyst" in output
    assert "REFUSED  export as analyst" in output
    assert "audit verified: True" in output
    assert "case snapshots remaining: 0" in output


def test_the_cli_reports_a_refusal_as_a_nonzero_exit(tmp_path, capsys):
    from gates.cli import main

    code = main(
        [
            "--root",
            str(tmp_path / "casework"),
            "authorise",
            "--case",
            "NOPE",
            "--actor",
            "x",
        ]
    )
    assert code == 2
    assert "REFUSED" in capsys.readouterr().out