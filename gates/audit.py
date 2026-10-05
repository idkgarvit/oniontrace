"""Append-only audit trail (Part E2.3).

Two properties are enforced here, and a third is stated as a limit:

1. **Append-only by construction.** Entries are appended as JSON Lines, each carrying the
   hash of its predecessor, so an edit, reorder or removal anywhere breaks verification
   from that point on. There is no update or delete method.
2. **Written by the gate, not by the analyst.** The write path is `_append`, called only
   by `CollectionGate` action methods. No permission grants a role the ability to write
   here, so an analyst cannot record their own justification for anything -- the record
   is a side effect of the action the gate performed, not something the actor supplies.
3. **Limit, stated not hidden.** This is tamper *evident*, not tamper *proof*. Anyone who
   can rewrite the whole file can recompute the chain. Proving absence of tampering needs
   each head hash shipped somewhere the operator cannot write (separate host, timestamping
   authority, WORM storage). The tool does not have that, and the report says so rather
   than describing a hash chain as if it were immutability.

The log lives in its own directory tree. Purging a case removes case data and leaves the
audit record of the purge, which is the only way "we deleted it, and logged that we did"
can be checked afterwards.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from gates.errors import AuditTampered
from gates.roles import Permission, Principal, require

#: Predecessor hash of the first entry.
GENESIS = "0" * 64


class AuditAction(str, Enum):
    """Every action E2.3 names, plus the gate's own refusals."""

    ACKNOWLEDGEMENT_RECORDED = "acknowledgement_recorded"
    CASE_OPENED = "case_opened"
    CASE_CLOSED = "case_closed"
    SEED_ADDED = "seed_added"
    COLLECTION_RUN = "collection_run"
    SNAPSHOT_STORED = "snapshot_stored"
    SNAPSHOT_VIEWED = "snapshot_viewed"
    QUERY = "query"
    LINK_CONFIRMED = "link_confirmed"
    LINK_REJECTED = "link_rejected"
    LINK_ANNOTATED = "link_annotated"
    LINK_ACTION_REVERSED = "link_action_reversed"
    EXPORT = "export"
    PURGE = "purge"
    GATE_REFUSED = "gate_refused"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _entry_hash(
    *,
    seq: int,
    recorded_at: str,
    actor: str,
    role: str,
    action: str,
    case_id: str | None,
    detail: dict[str, Any],
    prev_hash: str,
) -> str:
    """Hash of one entry, over a canonical serialisation of its own fields.

    `sort_keys` and compact separators matter: a hash over a differently-ordered
    serialisation would verify today and fail tomorrow.
    """
    payload = json.dumps(
        {
            "seq": seq,
            "recorded_at": recorded_at,
            "actor": actor,
            "role": role,
            "action": action,
            "case_id": case_id,
            "detail": detail,
            "prev_hash": prev_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AuditEntry:
    """One recorded action."""

    seq: int
    recorded_at: str
    actor: str
    role: str
    action: str
    case_id: str | None
    detail: dict[str, Any]
    prev_hash: str
    entry_hash: str

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AuditEntry":
        return cls(
            seq=raw["seq"],
            recorded_at=raw["recorded_at"],
            actor=raw["actor"],
            role=raw["role"],
            action=raw["action"],
            case_id=raw.get("case_id"),
            detail=raw.get("detail", {}),
            prev_hash=raw["prev_hash"],
            entry_hash=raw["entry_hash"],
        )

    def recompute_hash(self) -> str:
        return _entry_hash(
            seq=self.seq,
            recorded_at=self.recorded_at,
            actor=self.actor,
            role=self.role,
            action=self.action,
            case_id=self.case_id,
            detail=self.detail,
            prev_hash=self.prev_hash,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "recorded_at": self.recorded_at,
            "actor": self.actor,
            "role": self.role,
            "action": self.action,
            "case_id": self.case_id,
            "detail": self.detail,
            "prev_hash": self.prev_hash,
            "entry_hash": self.entry_hash,
        }


class AuditLog:
    """The append-only record. One file, one chain, no update path."""

    def __init__(self, root: Path, *, clock: Callable[[], str] = _utc_now) -> None:
        self.root = Path(root)
        self.path = self.root / "audit.jsonl"
        self._clock = clock
        self.root.mkdir(parents=True, exist_ok=True)

    # -- reading ----------------------------------------------------------- #

    def entries(self) -> tuple[AuditEntry, ...]:
        if not self.path.exists():
            return ()
        parsed: list[AuditEntry] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                parsed.append(AuditEntry.from_dict(json.loads(line)))
        return tuple(parsed)

    def for_case(self, case_id: str) -> tuple[AuditEntry, ...]:
        return tuple(e for e in self.entries() if e.case_id == case_id)

    def read(
        self,
        principal: Principal,
        *,
        case_id: str | None = None,
        limit: int | None = None,
    ) -> tuple[AuditEntry, ...]:
        """Read the trail. Admin only.

        Admin-only rather than role-open because the trail names who did what, including
        analysts: an operator able to read the trail of an investigation they are not on
        has been told something about it. Reading the audit is itself a disclosure, so it
        carries its own permission rather than riding on "analyst and above".
        """
        require(principal, Permission.READ_AUDIT)
        entries = self.for_case(case_id) if case_id else self.entries()
        return entries[-limit:] if limit else entries

    # -- verification ------------------------------------------------------ #

    def verify(self) -> dict[str, Any]:
        """Recompute the chain and report the first entry that does not match."""
        entries = self.entries()
        prev = GENESIS
        for entry in entries:
            if entry.prev_hash != prev:
                return {
                    "verified": False,
                    "entries_checked": entry.seq,
                    "failure": "broken chain link",
                    "first_bad_seq": entry.seq,
                }
            if entry.recompute_hash() != entry.entry_hash:
                return {
                    "verified": False,
                    "entries_checked": entry.seq,
                    "failure": "entry content does not match its recorded hash",
                    "first_bad_seq": entry.seq,
                }
            prev = entry.entry_hash
        return {
            "verified": True,
            "entries_checked": len(entries),
            "head_hash": prev,
        }

    def assert_untampered(self) -> None:
        result = self.verify()
        if not result["verified"]:
            raise AuditTampered(
                f"audit chain failed at entry {result['first_bad_seq']}: {result['failure']}"
            )

    # -- writing (gate internal) ------------------------------------------- #

    def _append(
        self,
        *,
        action: AuditAction,
        actor: Principal,
        case_id: str | None,
        detail: dict[str, Any],
    ) -> AuditEntry:
        """Append one entry. Called by `CollectionGate` only.

        Underscored on purpose: this is not a public capability. An audit trail an actor
        can write to is testimony, not evidence, so the gate -- not the actor -- decides
        what gets recorded, and the actor cannot supply a favourable version of events.
        """
        existing = self.entries()
        seq = len(existing)
        prev_hash = existing[-1].entry_hash if existing else GENESIS
        recorded_at = self._clock()
        entry_hash = _entry_hash(
            seq=seq,
            recorded_at=recorded_at,
            actor=actor.principal_id,
            role=actor.role.value,
            action=action.value,
            case_id=case_id,
            detail=detail,
            prev_hash=prev_hash,
        )
        entry = AuditEntry(
            seq=seq,
            recorded_at=recorded_at,
            actor=actor.principal_id,
            role=actor.role.value,
            action=action.value,
            case_id=case_id,
            detail=detail,
            prev_hash=prev_hash,
            entry_hash=entry_hash,
        )
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry.to_dict(), sort_keys=True, default=str) + "\n")
        return entry
