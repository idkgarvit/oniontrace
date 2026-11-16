"""The collection gate (Part E2.2) and the site of every E2 enforcement.

**No case, no collection.** This is enforced with a signature rather than a flag, because
a collector that can be started by passing `--force` is not gated. The gate mints a
:class:`CollectionPermit` naming the case, the principal and a short expiry, signed with a
key that lives in the gate root. The ingest path -- the seam the real crawler will call --
verifies that signature before it writes anything. A permit cannot be forged without the
key, cannot be reused after it expires, cannot be moved to another case, and cannot be
handed to a different principal, and each of those is separately tested.

**Where authentication stops.** This module decides what an authenticated principal may
do. It never establishes who they are: `Principal` is handed in from outside, and nothing
here reads a role from a flag, a file, or the environment. That boundary is the honest one
-- a role check is only as good as the identity behind it -- and the bundled CLI has no
authentication at all, which is why it is a demonstration path and says so.

Refusals are recorded too. A gate that logs only successes cannot answer "was anyone
trying to start collection into a case that had already been purged?".
"""

from __future__ import annotations

import logging

from . import permissions

logger = logging.getLogger(__name__)

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from typing import Callable, Sequence

from gates.acceptable_use import Acknowledgement, AcknowledgementStore
from gates.audit import AuditAction, AuditEntry, AuditLog
from gates.cases import Case, CaseStore, LinkDecision, _parse, utc_now
from gates.errors import GateRefused, IdentityAssertion, PermissionDenied
from gates.exports import ExportDecision, Watermark, build_evidence_pack
from gates.presentation import CandidateLink, render_abstention, render_candidate
from gates.roles import Permission, Principal, Role, require

#: How long a permit remains valid. Short on purpose: a permit is for one collection run,
#: not a session, so a stale one is a bug rather than a convenience.
PERMIT_TTL_MINUTES = 15


@dataclass(frozen=True)
class CollectionPermit:
    """Proof that a named case authorised a named principal to collect, until a moment."""

    case_id: str
    principal_id: str
    issued_at: str
    expires_at: str
    nonce: str
    signature: str

    def to_dict(self) -> dict[str, str]:
        return {
            "case_id": self.case_id,
            "principal_id": self.principal_id,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "nonce": self.nonce,
            "signature": self.signature,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, str]) -> "CollectionPermit":
        return cls(
            case_id=raw["case_id"],
            principal_id=raw["principal_id"],
            issued_at=raw["issued_at"],
            expires_at=raw["expires_at"],
            nonce=raw["nonce"],
            signature=raw["signature"],
        )

    def is_expired(self, now: str) -> bool:
        return _parse(now) >= _parse(self.expires_at)


class CollectionGate:
    """The single place a collection run, a query, an action or an export passes through."""

    def __init__(
        self,
        root: Path,
        *,
        confirm_threshold: float = 0.95,
        clock: Callable[[], str] = utc_now,
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.cases = CaseStore(self.root)
        # The audit lives in its own tree so that purging a case cannot touch the record
        # of the case. A shared directory would make "purged but logged" unverifiable.
        self.audit = AuditLog(self.root / "audit", clock=clock)
        self.acknowledgements = AcknowledgementStore(self.root)
        self.confirm_threshold = confirm_threshold
        self._clock = clock
        # A tree written before this module existed is the one that matters: it has been sitting
        # there with a world-readable permit key. Close it, and report what was open rather than
        # repairing it in silence -- an operator whose key was exposed needs to know.
        self.permission_repairs = permissions.harden_tree(self.root)
        if self.permission_repairs:
            logger.warning(
                "closed %d path(s) under %s that were readable or writable beyond their owner; "
                "the first few were: %s",
                len(self.permission_repairs), self.root,
                ", ".join(str(p.relative_to(self.root)) for p in self.permission_repairs[:5]),
            )

        self._key = self._load_key()

    # -- signing key ------------------------------------------------------- #

    def rotate_permit_key(self, principal: Principal, *, reason: str) -> dict[str, str]:
        """Replace the permit key, keep the old one as a record, and write down why.

        The key was readable beyond its owner for as long as nobody looked, and a key that has been
        readable cannot be told from a forged one -- `_load_key` says exactly that when it tightens
        the mode, and then offered nothing to do about it. Deleting the file would mint a fresh key
        on the next write, silently and without a record; this does the same thing deliberately,
        with a reason attached and the retired key kept.

        Every permit signed under the old key stops verifying. That is the point rather than a
        side effect: after an exposure, permits that some unknown party may have minted are
        precisely what has to stop working. A case mid-collection wants a new permit afterwards.

        Deliberately *not* gated on the acceptable-use acknowledgement, for the reason `purge` is
        not: an operator must always be able to answer a security problem, including when
        something else has already gone wrong.
        """

        try:
            require(principal, Permission.ROTATE_KEY)
        except GateRefused as error:
            self._refuse_now(principal, None, error)
            raise

        reason = (reason or "").strip()
        if not reason:
            raise GateRefused(
                "E2.3",
                "a rotation needs a reason: the trail is the only place a future reader can learn "
                "why every permit issued before this date stopped verifying",
            )

        path = self._key_path()
        if not path.exists():
            raise GateRefused(
                "E2.2",
                f"there is no permit key at {path} to rotate. One is created on first use, and a "
                "rotation records what it replaced, so there is nothing here to record.",
            )

        retired = bytes.fromhex(path.read_text(encoding="utf-8").strip())
        replacement = secrets.token_bytes(32)
        archived = self.root / f"permit.key.superseded-{self._clock()}"
        # Archive before overwriting: if the archive fails, the live key is untouched and the
        # rotation has simply not happened.
        permissions.secure_write_text(archived, retired.hex())
        permissions.secure_write_text(path, replacement.hex())
        self._key = replacement

        def fingerprint(key: bytes) -> str:
            """Enough to tell two keys apart in the trail, not enough to use one.

            The record says *that* the key changed and which one is which. Writing the key itself
            into the audit would put the secret in the file more people read.
            """

            return hashlib.sha256(key).hexdigest()[:16]

        self.audit._append(
            action=AuditAction.KEY_ROTATED,
            actor=principal,
            case_id=None,
            detail={
                "reason": reason,
                "retired_fingerprint": fingerprint(retired),
                "replacement_fingerprint": fingerprint(replacement),
                "retired_key_kept_as": archived.name,
            },
        )
        return {
            "retired_fingerprint": fingerprint(retired),
            "replacement_fingerprint": fingerprint(replacement),
            "retired_key_kept_as": str(archived),
        }

    def _key_path(self) -> Path:
        return self.root / "permit.key"

    def _load_key(self) -> bytes:
        """The HMAC key every permit is verified against.

        Written owner-only. `write_text` gave this file the process umask, which on this machine
        made it 0664: world-readable, so any local user could mint a permit for any case, and
        group-writable, so any of them could replace the key instead. Either one defeats the check
        the key exists to support, and a permit is this project's legal control.
        """
        path = self._key_path()
        if path.exists():
            if permissions._chmod(path, permissions.FILE_MODE):
                logger.warning(
                    "permit.key was readable beyond its owner; tightened to %s. Treat the key as "
                    "exposed: a permit signed with it cannot be told from a forged one.",
                    oct(permissions.FILE_MODE),
                )
            return bytes.fromhex(path.read_text(encoding="utf-8").strip())
        key = secrets.token_bytes(32)
        permissions.secure_write_text(path, key.hex())
        return key

    def _current_key(self) -> bytes:
        """The key on disk, re-read whenever it has changed.

        A rotation happens in another process: an operator runs `python -m gates key rotate` while
        a collector or the console is already up. Trusting the key this object read at construction
        means that process keeps honouring permits signed with the retired key until it restarts,
        which is the one outcome a rotation exists to prevent. One small file read per signature
        settles it, and signing is not the hot path -- collection is network-bound.

        A change is reported rather than absorbed, because "the key under me changed" is something
        an operator wants to be told.
        """

        path = self._key_path()
        if not path.exists():
            # No key at all: create or re-read one rather than verify against a key nobody holds.
            self._key = self._load_key()
            return self._key
        on_disk = bytes.fromhex(path.read_text(encoding="utf-8").strip())
        if on_disk != self._key:
            logger.warning(
                "permit.key changed on disk since this process started; using the current key. "
                "Permits signed under the previous key no longer verify."
            )
            self._key = on_disk
        return self._key

    def _sign(self, permit: CollectionPermit) -> str:
        payload = json.dumps(
            {
                "case_id": permit.case_id,
                "principal_id": permit.principal_id,
                "issued_at": permit.issued_at,
                "expires_at": permit.expires_at,
                "nonce": permit.nonce,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hmac.new(self._current_key(), payload.encode("utf-8"), hashlib.sha256).hexdigest()

    # -- acknowledgements (E2.6) ------------------------------------------- #

    def acknowledge(self, principal: Principal) -> Acknowledgement:
        """Record acceptance of ACCEPTABLE_USE.md, once per deployment.

        Not permission-gated: accepting the terms is not a privileged act, and requiring a
        role to acknowledge would mean a junior operator could not start the deployment
        they are about to use.
        """
        record = self.acknowledgements.record(principal, now=self._clock())
        self.audit._append(
            action=AuditAction.ACKNOWLEDGEMENT_RECORDED,
            actor=principal,
            case_id=None,
            detail={"statement_digest": record.statement_digest},
        )
        return record

    # -- refusals are recorded -------------------------------------------- #

    def _refuse_now(
        self, principal: Principal | None, case_id: str | None, error: Exception
    ) -> None:
        """Record a refusal, then re-raise.

        Logged with the rule that fired, so an attempt is visible even though nothing
        happened -- the only way "did anyone try?" has an answer.
        """
        actor = principal or Principal(principal_id="unknown", role=Role.VIEWER)
        self.audit._append(
            action=AuditAction.GATE_REFUSED,
            actor=actor,
            case_id=case_id,
            detail={"error": str(error), "error_type": type(error).__name__},
        )

    def _require_owner(self, principal: Principal, case: Case) -> None:
        """Case-ownership check for case-scoped permissions.

        Holding the case-owner role must not confer authority over someone else's case, or
        the role would just mean "trusted person" and the case scoping would be
        decorative. Admins are exempt because someone has to be able to resolve a dispute.
        """
        if principal.role is Role.ADMIN:
            return
        if case.owner != principal.principal_id:
            raise PermissionDenied(
                principal_id=principal.principal_id,
                role=principal.role.value,
                permission="case ownership",
                required=f"owner of {case.case_id!r} (owned by {case.owner!r})",
            )

    # -- cases (E2.2) ------------------------------------------------------ #

    def open_case(
        self,
        principal: Principal,
        *,
        case_id: str,
        title: str,
        authorisation_ref: str,
        retention_days: int,
        owner: str | None = None,
    ) -> Case:
        """Open a case. Requires the statement to be acknowledged first."""
        try:
            self.acknowledgements.require()
            require(principal, Permission.OPEN_CASE)
            # Only an admin may open a case owned by somebody else; otherwise a case
            # owner could manufacture authority in another analyst's name.
            owner_id = owner or principal.principal_id
            if owner_id != principal.principal_id and principal.role is not Role.ADMIN:
                raise PermissionDenied(
                    principal_id=principal.principal_id,
                    role=principal.role.value,
                    permission="open a case for another analyst",
                    required="admin",
                )
            case = self.cases.create(
                case_id=case_id,
                title=title,
                owner=owner_id,
                authorisation_ref=authorisation_ref,
                retention_days=retention_days,
                now=self._clock(),
            )
        except GateRefused as error:
            self._refuse_now(principal, case_id, error)
            raise

        self.audit._append(
            action=AuditAction.CASE_OPENED,
            actor=principal,
            case_id=case.case_id,
            detail={
                "title": case.title,
                "owner": case.owner,
                "authorisation_ref": case.authorisation_ref,
                "retention_until": case.retention_until,
            },
        )
        return case

    def add_seed(self, case_id: str, seed: str, principal: Principal) -> Case:
        """Add a seed to one case. Needs the case to be collectable, not just to exist."""
        try:
            require(principal, Permission.ADD_SEED)
            self.acknowledgements.require()
            self._require_owner(principal, self.cases.get(case_id))
            case = self.cases.add_seed(case_id, seed)
        except GateRefused as error:
            self._refuse_now(principal, case_id, error)
            raise

        self.audit._append(
            action=AuditAction.SEED_ADDED,
            actor=principal,
            case_id=case_id,
            detail={"seed": seed.strip(), "seed_count": len(case.seeds)},
        )
        return case

    # -- the collection gate (E2.2) ---------------------------------------- #

    def authorise_collection(self, case_id: str, principal: Principal) -> CollectionPermit:
        """Mint a permit for one collection run, or refuse.

        The four refusals here *are* the E2.2 requirement in executable form: no
        acknowledgement, no permission, not the case owner, no such case, case purged or
        sealed, retention expired.
        """
        try:
            self.acknowledgements.require()
            require(principal, Permission.START_COLLECTION)
            case = self.cases.require_collectable(case_id)
            self._require_owner(principal, case)
            now = self._clock()
            if case.is_expired(now):
                raise GateRefused(
                    "E2.2",
                    f"case {case_id!r} retention expired at {case.retention_until}; "
                    "collecting into an expired case is the retention rule being ignored",
                )
            permit = CollectionPermit(
                case_id=case_id,
                principal_id=principal.principal_id,
                issued_at=now,
                expires_at=(
                    _parse(now) + timedelta(minutes=PERMIT_TTL_MINUTES)
                ).isoformat(timespec="seconds"),
                nonce=secrets.token_hex(8),
                signature="",
            )
            permit = replace(permit, signature=self._sign(permit))
        except GateRefused as error:
            self._refuse_now(principal, case_id, error)
            raise
        return permit

    def verify_permit(self, permit: CollectionPermit, principal: Principal) -> None:
        """Check a permit's signature, expiry and bindings, or refuse."""
        if not hmac.compare_digest(self._sign(permit), permit.signature):
            raise GateRefused(
                "E2.2",
                "collection permit signature does not verify; it was not issued by this "
                "gate or its contents were altered after issue",
            )
        if permit.principal_id != principal.principal_id:
            raise GateRefused(
                "E2.2",
                f"permit was issued to {permit.principal_id!r} and is being presented by "
                f"{principal.principal_id!r}",
            )
        if permit.is_expired(self._clock()):
            raise GateRefused(
                "E2.2",
                f"permit for case {permit.case_id!r} expired at {permit.expires_at}",
            )
        # The case is re-checked at use, not only at issue: it may have been sealed or
        # purged in between, and a permit is not a licence to write into a closed case.
        self.cases.require_collectable(permit.case_id)

    def ingest(
        self,
        permit: CollectionPermit,
        principal: Principal,
        payloads: Sequence[dict],
    ) -> tuple[str, ...]:
        """Store collected snapshots. This is the seam the real crawler calls.

        Everything a caller must hold is checked here before a byte is written, which is
        what makes "no case, no collection" a property of the code rather than of the
        documentation. The crawl itself does not exist yet (WP2.2); what exists is the
        gate it will have to satisfy.
        """
        try:
            self.verify_permit(permit, principal)
            require(principal, Permission.START_COLLECTION)
        except GateRefused as error:
            self._refuse_now(principal, permit.case_id, error)
            raise

        names = tuple(
            self.cases.store_snapshot(permit.case_id, payload) for payload in payloads
        )
        self.audit._append(
            action=AuditAction.COLLECTION_RUN,
            actor=principal,
            case_id=permit.case_id,
            detail={
                "snapshots": len(names),
                "permit_nonce": permit.nonce,
                "permit_expires_at": permit.expires_at,
            },
        )
        for name in names:
            self.audit._append(
                action=AuditAction.SNAPSHOT_STORED,
                actor=principal,
                case_id=permit.case_id,
                detail={"snapshot": name},
            )
        return names

    def collected_snapshots(
        self, principal: Principal, case_id: str
    ) -> tuple[dict, ...]:
        """Read back what a case actually holds. Analyst and above, own case only.

        Two things are worth stating about this method, because E2.3's checklist does not
        anticipate either:

        * Acquisition calls it to rebuild its dedupe ledger from the stored bytes rather
          than from its own counter. A collector that trusted its own memory would skip
          messages it believed it had stored but had not, leaving a silent permanent gap.
        * Reading raw collected content is a disclosure event, so it is recorded. E2.3
          names no read action; adding one is the honest answer to that gap, and leaving
          the read invisible because the checklist omitted it is not.
        """
        try:
            require(principal, Permission.VIEW_SNAPSHOTS)
            case = self.cases.get(case_id)
            self._require_owner(principal, case)
        except GateRefused as error:
            self._refuse_now(principal, case_id, error)
            raise
        snapshots = self.cases.read_snapshots(case_id)
        self.audit._append(
            action=AuditAction.SNAPSHOT_VIEWED,
            actor=principal,
            case_id=case_id,
            detail={"snapshots": len(snapshots)},
        )
        return snapshots

    # -- analyst activity (E2.3, E2.5) ------------------------------------- #

    def record_query(self, case_id: str, query: str, principal: Principal) -> AuditEntry:
        """Record that a query was run. The query text is part of the record.

        Stored rather than hashed: an audit trail that says "a query happened" without
        saying what was asked cannot answer the question the trail exists for. Reading it
        is restricted to admin, which is why recording it is acceptable.
        """
        try:
            require(principal, Permission.RUN_QUERY)
            self.acknowledgements.require()
            self._require_owner(principal, self.cases.get(case_id))
        except GateRefused as error:
            self._refuse_now(principal, case_id, error)
            raise

        return self.audit._append(
            action=AuditAction.QUERY,
            actor=principal,
            case_id=case_id,
            detail={"query": query},
        )

    def view_candidates(
        self, principal: Principal, case_id: str, links: Sequence[CandidateLink]
    ) -> tuple[str, ...]:
        """Render candidate links for a principal, or refuse.

        Viewers are refused outright rather than shown a redacted view: a redacted
        candidate link is still a link, and E2.4 puts candidates at analyst or above.
        """
        require(principal, Permission.VIEW_CANDIDATE_LINKS)
        rendered = []
        for link in links:
            decision = self.cases.decision_for(case_id, link.left, link.right)
            rendered.append(
                render_candidate(
                    link,
                    confirm_threshold=self.confirm_threshold,
                    decision=decision.action if decision else None,
                )
            )
        return tuple(rendered)

    def render_links_or_abstain(
        self, principal: Principal, case_id: str, links: Sequence[CandidateLink]
    ) -> tuple[str, ...]:
        """What a view shows: the candidates, or the abstention state.

        Both come back through the same permission check, so there is no path on which an
        empty result renders as a blank panel to a principal who may not see results.
        """
        require(principal, Permission.VIEW_CANDIDATE_LINKS)
        if not links:
            return (
                render_abstention(case_id, confirm_threshold=self.confirm_threshold),
            )
        return self.view_candidates(principal, case_id, links)

    # -- analyst actions on candidates (E2.3, E2.5) ------------------------ #

    def _preconditions_for_action(self, case_id: str, principal: Principal) -> Case:
        """Shared precondition block for actions that touch case data."""
        require(principal, Permission.RECORD_LINK_ACTION)
        self.acknowledgements.require()
        case = self.cases.get(case_id)
        self._require_owner(principal, case)
        return case

    def confirm_link(
        self, case_id: str, link: CandidateLink, principal: Principal
    ) -> LinkDecision:
        """Record an analyst's confirmation. The only route to the confirmed state."""
        try:
            self._preconditions_for_action(case_id, principal)
        except GateRefused as error:
            self._refuse_now(principal, case_id, error)
            raise
        entry = self.audit._append(
            action=AuditAction.LINK_CONFIRMED,
            actor=principal,
            case_id=case_id,
            detail={
                "left": link.left,
                "right": link.right,
                "score": link.score,
                "method": link.method,
                "evidence": list(link.evidence_refs),
            },
        )
        decision = LinkDecision(
            action="confirmed",
            decided_by=principal.principal_id,
            recorded_at=self._clock(),
            audit_seq=entry.seq,
        )
        self.cases.record_decision(case_id, link.left, link.right, decision)
        return decision

    def reject_link(
        self,
        case_id: str,
        link: CandidateLink,
        principal: Principal,
        *,
        reason: str | None = None,
    ) -> LinkDecision:
        """Record an analyst's rejection. Counter-evidence has somewhere to go."""
        try:
            self._preconditions_for_action(case_id, principal)
        except GateRefused as error:
            self._refuse_now(principal, case_id, error)
            raise
        entry = self.audit._append(
            action=AuditAction.LINK_REJECTED,
            actor=principal,
            case_id=case_id,
            detail={
                "left": link.left,
                "right": link.right,
                "score": link.score,
                "method": link.method,
                "reason": reason,
            },
        )
        decision = LinkDecision(
            action="rejected",
            decided_by=principal.principal_id,
            recorded_at=self._clock(),
            reason=reason,
            audit_seq=entry.seq,
        )
        self.cases.record_decision(case_id, link.left, link.right, decision)
        return decision

    def annotate_link(
        self, case_id: str, link: CandidateLink, note: str, principal: Principal
    ) -> AuditEntry:
        """Record a free-text note. Logged, but it does not change the link's state."""
        try:
            self._preconditions_for_action(case_id, principal)
        except GateRefused as error:
            self._refuse_now(principal, case_id, error)
            raise
        return self.audit._append(
            action=AuditAction.LINK_ANNOTATED,
            actor=principal,
            case_id=case_id,
            detail={"left": link.left, "right": link.right, "note": note},
        )

    def reverse_link_action(
        self,
        case_id: str,
        link: CandidateLink,
        principal: Principal,
        *,
        reason: str,
    ) -> LinkDecision | None:
        """Undo an analyst's decision on a link, with a reason that is not optional.

        Reversibility is a requirement (E2.3) and so is logging the reversal. A reversal
        with no reason cannot be told apart from a mistake, so an empty reason is refused
        rather than stored as `None` and left to look like one.
        """
        if not reason or not reason.strip():
            error = GateRefused(
                "E2.3",
                "a reversal must carry a reason; an unexplained reversal is "
                "indistinguishable from an error",
            )
            self._refuse_now(principal, case_id, error)
            raise error
        try:
            self._preconditions_for_action(case_id, principal)
        except GateRefused as error:  # pragma: no cover - defensive re-raise
            self._refuse_now(principal, case_id, error)
            raise

        removed = self.cases.clear_decision(case_id, link.left, link.right)
        self.audit._append(
            action=AuditAction.LINK_ACTION_REVERSED,
            actor=principal,
            case_id=case_id,
            detail={
                "left": link.left,
                "right": link.right,
                "reason": reason.strip(),
                "reversed_action": removed.action if removed else None,
                "reversed_decided_by": removed.decided_by if removed else None,
                "reversed_audit_seq": removed.audit_seq if removed else None,
            },
        )
        return removed

    # -- export (E2.4) ----------------------------------------------------- #

    def export_evidence_pack(
        self,
        case_id: str,
        principal: Principal,
        links: Sequence[CandidateLink],
        *,
        snapshot_count: int = 0,
    ) -> str:
        """Build a watermarked evidence pack and record the export.

        The retrieval reference is derived from the audit entry's own sequence number, so
        a page in someone's hands resolves to the action that produced it. The pack is
        built *before* the export is logged, so a pack that fails the identity-assertion
        lint leaves no export on the record.
        """
        try:
            require(principal, Permission.EXPORT)
            self.acknowledgements.require()
            case = self.cases.get(case_id)
            self._require_owner(principal, case)
            if case.purged:
                raise GateRefused("E2.2", f"case {case_id!r} was purged; nothing to export")
        except GateRefused as error:
            self._refuse_now(principal, case_id, error)
            raise

        generated_at = self._clock()
        next_seq = len(self.audit.entries())
        digest = hashlib.sha256(
            f"{case_id}:{principal.principal_id}:{generated_at}:{next_seq}".encode("utf-8")
        ).hexdigest()[:8]
        reference = f"ONION-{case_id}-{next_seq:06d}-{digest}"

        decisions: list[ExportDecision] = []
        for link in links:
            record = self.cases.decision_for(case_id, link.left, link.right)
            decisions.append(
                ExportDecision(
                    link=link,
                    decision=record.action if record else None,
                    decided_by=record.decided_by if record else None,
                    reason=record.reason if record else None,
                )
            )

        watermark = Watermark(
            case_id=case_id,
            analyst_id=principal.principal_id,
            generated_at=generated_at,
            retrieval_reference=reference,
        )
        try:
            pack = build_evidence_pack(
                case=case,
                watermark=watermark,
                decisions=decisions,
                confirm_threshold=self.confirm_threshold,
                snapshot_count=snapshot_count,
            )
        except IdentityAssertion as error:
            self._refuse_now(principal, case_id, error)
            raise

        self.audit._append(
            action=AuditAction.EXPORT,
            actor=principal,
            case_id=case_id,
            detail={
                "retrieval_reference": reference,
                "candidates": len(links),
                "watermark": watermark.as_line(),
            },
        )
        return pack

    # -- purge (E2.2, E2.4) ------------------------------------------------ #

    def purge(self, case_id: str, principal: Principal) -> Case:
        """Delete a case's data, keeping the tombstone and logging the deletion.

        Deliberately *not* gated on the acceptable-use acknowledgement. An un-acknowledged
        deployment should not hold cases, but if it somehow does, refusing to purge would
        mean refusing to comply with retention -- an operator must always be able to
        delete data, including when something else has gone wrong.
        """
        try:
            require(principal, Permission.PURGE)
            self.cases.get(case_id)
        except GateRefused as error:
            self._refuse_now(principal, case_id, error)
            raise

        purged = self.cases.purge(case_id, now=self._clock())
        self.audit._append(
            action=AuditAction.PURGE,
            actor=principal,
            case_id=case_id,
            detail={
                "removed_snapshots": purged.purged_snapshots,
                "purged_at": purged.purged_at,
                "authorisation_ref": purged.authorisation_ref,
                "trigger": "explicit",
            },
        )
        return purged

    def sweep_expired(
        self, principal: Principal, *, now: str | None = None
    ) -> tuple[str, ...]:
        """Purge every case whose retention has run out. Admin only.

        A retention policy that is only enforced by remembering to enforce it is not a
        policy, so expiry is a sweep that can be run (and tested) rather than a note in
        the documentation telling someone to delete things.
        """
        require(principal, Permission.PURGE)
        moment = now or self._clock()
        expired = self.cases.expired(moment)
        for case_id in expired:
            purged = self.cases.purge(case_id, now=moment)
            self.audit._append(
                action=AuditAction.PURGE,
                actor=principal,
                case_id=case_id,
                detail={
                    "removed_snapshots": purged.purged_snapshots,
                    "purged_at": purged.purged_at,
                    "retention_until": purged.retention_until,
                    "trigger": "retention_expiry",
                },
            )
        return expired